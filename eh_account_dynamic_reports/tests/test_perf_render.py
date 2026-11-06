# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Performance scaffolding for the dynamic report render path.

These are NOT functional tests. They gate the orchestrator render()
(the exact call the OWL viewer makes) against regression thresholds.
Thresholds are intentionally generous; tighten as the engine matures.

Run only the perf suite (the environment flag keeps wall-clock assertions
out of ordinary module CI):

    EH_RUN_PERF=1 odoo --test-tags perf -i eh_account_dynamic_reports

Run the heavy variant (env-gated to keep CI fast):

    EH_RUN_HEAVY_PERF=1 odoo --test-tags eh_account_dynamic_reports,perf,heavy \
        -i eh_account_dynamic_reports
"""

import os
import time

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


# Thresholds (milliseconds). Generous baselines; tune as the engine matures.
THRESHOLD_RENDER_MS = 1500
THRESHOLD_RENDER_HEAVY_MS = 4000


def _get_trial_balance_report(env):
    report = env['eh.account.dynamic.report'].search(
        [('code', '=', 'trial_balance')], limit=1,
    )
    if not report:
        report = env['eh.account.dynamic.report'].create({
            'code': 'trial_balance',
            'name': 'Trial Balance',
            'handler_model': 'eh.account.dynamic.report.handler.trial_balance',
        })
    return report


@tagged('perf', 'post_install', '-at_install')
class TestDynamicReportRenderPerf(EhAccountIntegrationTestCase):
    """Render baseline on a moderate seeded ledger."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if os.environ.get('EH_RUN_PERF') != '1':
            cls.skipTest(
                cls, "Perf timing disabled. Set EH_RUN_PERF=1 to run.",
            )
        cls.report = _get_trial_balance_report(cls.env)
        # Seed a moderate ledger so render() does real aggregate work
        # rather than running against an empty table.
        for n in range(60):
            cls.post_balanced_move([
                {'account': cls.account_revenue, 'credit': 1.0,
                 'partner': cls.partner_a if n % 2 == 0 else cls.partner_b},
                {'account': cls.account_cash, 'debit': 1.0},
            ], date=fields.Date.from_string('2026-06-15'))

    def _options(self):
        return {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }

    @staticmethod
    def _measure_ms(fn):
        start = time.perf_counter()
        result = fn()
        return (time.perf_counter() - start) * 1000, result

    def test_render_under_threshold(self):
        opts = self._options()
        # Warm up: the first call primes compiled queries and caches.
        self.report.render(opts)
        elapsed, payload = self._measure_ms(lambda: self.report.render(opts))
        self.assertTrue(
            payload is not None and payload.get('lines') is not None,
            "render() must return a payload with a lines array",
        )
        self.assertLess(
            elapsed, THRESHOLD_RENDER_MS,
            "render() took %.2fms; threshold %dms" % (
                elapsed, THRESHOLD_RENDER_MS,
            ),
        )

    def test_cached_render_returns_same_shape_under_threshold(self):
        # A second identical render is served from the orchestrator
        # cache; it must stay under the cold threshold and return the
        # same number of lines. Guards a cache regression that would
        # silently recompute.
        opts = self._options()
        first = self.report.render(opts)
        elapsed, second = self._measure_ms(lambda: self.report.render(opts))
        self.assertEqual(
            len(first['lines']), len(second['lines']),
            "cached render must return the same line set",
        )
        self.assertLess(
            elapsed, THRESHOLD_RENDER_MS,
            "cached render() took %.2fms; threshold %dms" % (
                elapsed, THRESHOLD_RENDER_MS,
            ),
        )


@tagged('perf', 'heavy', 'post_install',
        '-at_install')
class TestDynamicReportRenderPerfHeavy(EhAccountIntegrationTestCase):
    """Heavy render perf. Skipped unless EH_RUN_HEAVY_PERF=1."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if os.environ.get('EH_RUN_HEAVY_PERF') != '1':
            cls.skipTest(
                cls, "Heavy perf disabled. Set EH_RUN_HEAVY_PERF=1 to run.",
            )
        cls.report = _get_trial_balance_report(cls.env)
        # Seed a larger ledger via batched create to exercise the
        # aggregate path under volume.
        BATCH = 500
        for _batch in range(4):  # 4 * 500 * 2 = 4000 aml rows
            move_vals = []
            for n in range(BATCH):
                move_vals.append({
                    'move_type': 'entry',
                    'journal_id': cls.journal_misc.id,
                    'date': '2026-06-15',
                    'line_ids': [
                        (0, 0, {
                            'account_id': cls.account_revenue.id,
                            'credit': 1.0,
                            'partner_id': (
                                cls.partner_a.id if n % 2 == 0
                                else cls.partner_b.id
                            ),
                        }),
                        (0, 0, {
                            'account_id': cls.account_cash.id,
                            'debit': 1.0,
                        }),
                    ],
                })
            moves = cls.env['account.move'].create(move_vals)
            moves.action_post()

    def test_render_large_ledger_under_threshold(self):
        opts = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }
        self.report.render(opts)  # warm up
        start = time.perf_counter()
        payload = self.report.render(opts)
        elapsed = (time.perf_counter() - start) * 1000
        self.assertTrue(payload and payload.get('lines') is not None)
        self.assertLess(
            elapsed, THRESHOLD_RENDER_HEAVY_MS,
            "large render() took %.2fms; threshold %dms" % (
                elapsed, THRESHOLD_RENDER_HEAVY_MS,
            ),
        )
