# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Performance regression guards.

These tests pin a query budget on the hot paths so a future refactor that
introduces N+1 queries fails the build instead of silently slowing things
down.

Numbers below are conservative (slightly above the observed baseline) so
the suite does not flake on the first slow CI worker. Tighten over time
once the baseline is stable.
"""

from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'performance', 'post_install', '-at_install')
class TestAssetPerformance(EhAssetTestCase):

    def test_query_budget_compute_schedule_36_months(self):
        """Generating a 36 line straight line schedule must stay
        under a fixed query budget."""
        asset = self._make_asset(
            code='PERF-SCH-1',
            acquisition_cost=36000.0,
            useful_life_months=36,
            method='straight_line',
            prorate_first_period=False,
        )
        # Batch create currently measures 9 queries on O19. A ceiling of 40
        # leaves cross-series/CI headroom but cannot hide one ORM create per
        # schedule row.
        with self.assertQueryCount(__system__=40):
            asset.action_compute_schedule()
        self.assertEqual(len(asset.depreciation_line_ids), 36)

    def test_query_budget_activate_with_existing_schedule(self):
        """Activation with a pre-built schedule should be light."""
        asset = self._make_asset(code='PERF-ACT-1')
        asset.action_compute_schedule()
        with self.assertQueryCount(__system__=80):
            asset.action_activate()
        self.assertEqual(asset.state, 'running')

    def test_query_budget_post_one_due_line(self):
        """Posting one due depreciation line must fit a fixed budget."""
        asset = self._make_asset(
            code='PERF-POST-1',
            in_service_date='2026-01-31',
            acquisition_cost=12000.0,
            useful_life_months=36,
        )
        asset.action_activate()
        first_line = asset.depreciation_line_ids.sorted('sequence')[0]
        # Full-suite installs add independent account.move posting guards.
        # Keep isolated-module budget tight while pinning combined-suite
        # overhead to a still-bounded ceiling. The O17 combined MRO measures
        # 220 queries; use the loaded borrowing sub-ledger model as a concrete
        # guard instead of the optional aggregate meta-module's install state.
        has_cross_module_move_guards = bool(
            self.env.registry.get('eh.borrowing.cost')
        )
        query_budget = 260 if has_cross_module_move_guards else 200
        with self.assertQueryCount(__system__=query_budget):
            first_line.action_post()
        self.assertTrue(first_line.is_posted)


@tagged('eh_account_assets_pro', 'performance', 'post_install', '-at_install')
class TestLeasePerformance(EhAssetTestCase):

    def test_query_budget_build_lease_schedule_36_periods(self):
        """36 month monthly lease schedule must stay under budget."""
        lease = self._make_lease(
            term_months=36, cadence='monthly',
            payment_amount=1000.0,
        )
        # The merged monthly ROU/payment schedule is created in one batch.
        # Pin a ceiling below the 36-row cardinality plus normal setup work so
        # per-row create cannot return unnoticed.
        with self.assertQueryCount(__system__=50):
            lease.action_compute_schedule()
        self.assertEqual(len(lease.schedule_line_ids), 36)

    def test_query_budget_activate_lease(self):
        """Lease activation posts an opening entry under budget."""
        lease = self._make_lease(
            term_months=12, cadence='monthly',
            payment_amount=1000.0,
        )
        with self.assertQueryCount(__system__=400):
            lease.action_activate()
        self.assertEqual(lease.state, 'active')
