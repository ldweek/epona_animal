# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Regression: sealed depreciation / lease / low-value-pool schedule lines must
not be re-armed and re-posted, and posting must be idempotent under a
concurrent cron / manual double-submit.

Findings addressed:

* is_posted / move_id are workflow-guarded, so a plain user cannot flip
  is_posted True->False over RPC to make the daily cron (or Post Due Lines)
  book a SECOND journal entry for a period already booked, nor repoint
  move_id at another entry.
* action_post is idempotent: a line that already carries a live posted move
  is skipped, so a double-submit / re-run does not duplicate the GL charge.
* the low-value pool books its annual charge at the pool year's fiscal
  year-end, not on the day the button happens to be clicked.

The guard is a no-op for a superuser (env.su); the test env runs as
SUPERUSER, so the negative paths are exercised with_user(a plain, non-manager
accounting user).
"""

from datetime import date

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'post_install', '-at_install')
class TestDoublePostGuard(EhAssetTestCase):

    def setUp(self):
        super().setUp()
        self.clerk = self._make_non_manager_user()

    # ---- helpers ----

    def _posted_depreciation_line(self):
        asset = self._make_asset(
            code='DPG-DEP',
            in_service_date='2025-12-31',
            acquisition_cost=120000.0,
            useful_life_months=120,
        )
        asset.action_activate()
        asset.action_post_due_lines()
        line = asset.depreciation_line_ids.filtered(lambda l: l.is_posted)[:1]
        self.assertTrue(line, "expected a posted depreciation line")
        return asset, line

    def _posted_lease_line(self):
        lease = self._make_lease(
            reference='DPG-LSE',
            commencement_date='2025-01-31',
            term_months=12, cadence='monthly',
            payment_timing='arrears', payment_amount=1000.0,
        )
        lease.action_activate()
        lease.action_post_due_lines()
        line = lease.schedule_line_ids.filtered(
            lambda l: l.is_posted).sorted('sequence')[:1]
        self.assertTrue(line, "expected a posted lease line")
        return lease, line

    def _pool(self):
        pool_account = self._ensure_account(
            self.env, '1541', 'LVP Pool DPG', 'asset_fixed',
        )
        return self.env['eh.asset.lvp.pool'].create({
            'name': 'LVP DPG',
            'company_id': self.company.id,
            'threshold': 1000.0,
            'pool_account_id': pool_account.id,
            'accumulated_account_id': self.account_accum_dep.id,
            'expense_account_id': self.account_dep_expense.id,
            'journal_id': self.journal_misc.id,
        })

    def _posted_pool_line(self, year=None):
        year = year or fields.Date.context_today(self.env['eh.asset']).year
        pool = self._pool()
        small = self._make_asset(
            code='LV-DPG',
            acquisition_cost=1000.0,
            in_service_date='%s-03-15' % year,
        )
        pool.action_transfer_asset(small)
        line = pool.action_compute_year(year=year)
        line.action_post()
        self.assertTrue(line.is_posted)
        return pool, line

    def _move_count(self):
        return self.env['account.move'].search_count(
            [('journal_id', '=', self.journal_misc.id)])

    @staticmethod
    def _direct_write_value(record, field_name):
        value = record[field_name]
        if record._fields[field_name].type == 'many2one':
            return value.id or False
        return value

    # ---- finding 1: guarded is_posted / move_id cannot be reset by a user --

    def test_depreciation_is_posted_reset_blocked(self):
        _asset, line = self._posted_depreciation_line()
        # Flipping is_posted back to False would re-arm the daily cron to book
        # a second depreciation move for the same period.
        with self.assertRaises(AccessError):
            line.with_user(self.clerk).write({'is_posted': False})
        # Repointing move_id is equally blocked.
        with self.assertRaises(AccessError):
            line.with_user(self.clerk).write({'move_id': False})
        self.assertTrue(line.is_posted)

    def test_lease_is_posted_reset_blocked(self):
        _lease, line = self._posted_lease_line()
        with self.assertRaises(AccessError):
            line.with_user(self.clerk).write({'is_posted': False})
        self.assertTrue(line.is_posted)

    def test_lvp_is_posted_reset_blocked(self):
        _pool, line = self._posted_pool_line()
        with self.assertRaises(AccessError):
            line.with_user(self.clerk).write({'is_posted': False})
        self.assertTrue(line.is_posted)

    # ---- finding 3: action_post is idempotent (no duplicate move) ----

    def test_depreciation_repost_is_noop(self):
        _asset, line = self._posted_depreciation_line()
        move = line.move_id
        before = self._move_count()
        # A re-run (cron + manual race, double-submit, retry) must not book a
        # second move for an already-posted line.
        line.action_post()
        self.assertEqual(self._move_count(), before)
        self.assertEqual(line.move_id, move)

    def test_lease_repost_is_noop(self):
        _lease, line = self._posted_lease_line()
        move = line.move_id
        before = self._move_count()
        line.action_post()
        self.assertEqual(self._move_count(), before)
        self.assertEqual(line.move_id, move)

    def test_lvp_repost_is_noop(self):
        _pool, line = self._posted_pool_line()
        move = line.move_id
        before = self._move_count()
        line.action_post()
        self.assertEqual(self._move_count(), before)
        self.assertEqual(line.move_id, move)

    # ---- the guard must not break the sanctioned poster ----

    def test_manager_posting_still_stamps_line(self):
        _asset, line = self._posted_depreciation_line()
        self.assertTrue(line.is_posted)
        self.assertTrue(line.move_id)
        self.assertEqual(line.move_id.state, 'posted')

    # ---- lease measurement frozen once posted (principal/interest gap) ----

    def test_lease_measurement_frozen_after_post(self):
        _lease, line = self._posted_lease_line()
        with self.assertRaises(UserError):
            line.principal = line.principal + 10.0
        with self.assertRaises(UserError):
            line.interest = line.interest + 10.0

    def test_all_booked_line_sources_amounts_and_audit_evidence_are_immutable(self):
        """Direct RPC cannot re-parent, remeasure, or delete booked rows."""
        _asset, depreciation = self._posted_depreciation_line()
        lease, lease_line = self._posted_lease_line()
        pool, pool_line = self._posted_pool_line()

        for line in (depreciation, lease_line, pool_line):
            for field_name in line._FROZEN_AFTER_POST:
                with self.subTest(model=line._name, field=field_name):
                    value = self._direct_write_value(line, field_name)
                    with self.assertRaises(UserError):
                        line.with_user(self.clerk).write({field_name: value})

        # Audit stamps and move links are guarded even when the attempted
        # value is a no-op; readonly in the browser alone is insufficient.
        for line in (depreciation, lease_line):
            for field_name in ('posted_at', 'posted_by_id', 'move_id'):
                with self.subTest(model=line._name, audit_field=field_name):
                    value = self._direct_write_value(line, field_name)
                    with self.assertRaises(AccessError):
                        line.with_user(self.clerk).write({field_name: value})

        for line in (depreciation, lease_line, pool_line):
            with self.subTest(model=line._name, operation='unlink'):
                with self.assertRaises(UserError):
                    line.unlink()

        # Parent cascades cannot be used to bypass the child unlink guard.
        with self.assertRaises(UserError):
            lease.unlink()
        with self.assertRaises(UserError):
            pool.unlink()

    def test_unbooked_schedule_rows_remain_deletable(self):
        asset = self._make_asset(code='DPG-DRAFT-DEP')
        asset.action_compute_schedule()
        depreciation = asset.depreciation_line_ids[:1]
        self.assertFalse(depreciation.is_posted)
        depreciation.unlink()

        lease = self._make_lease(reference='DPG-DRAFT-LEASE')
        lease.action_compute_schedule()
        lease_line = lease.schedule_line_ids[:1]
        self.assertFalse(lease_line.is_posted)
        lease_line.unlink()

        pool = self._pool()
        pool_line = self.env['eh.asset.lvp.pool.line'].create({
            'pool_id': pool.id,
            'year': 2027,
            'amount': 1.0,
        })
        pool_line.unlink()

    # ---- finding 4: LVP posts at the pool year's fiscal year-end ----

    def test_lvp_posts_at_fiscal_year_end_not_today(self):
        year = 2026
        _pool, line = self._posted_pool_line(year)
        expected = self.company.compute_fiscalyear_dates(
            date(year, 6, 30))['date_to']
        self.assertEqual(line.move_id.date, expected)
