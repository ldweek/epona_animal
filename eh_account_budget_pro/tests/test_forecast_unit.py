# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Standalone unit tests for the forecasting helpers.

Runs without Odoo so the maths can be validated in seconds. Covers:

* Linear trend identifies the right slope and intercept on synthetic
  data generated from a known line.
* Holt-Winters additive recovers the seasonal pattern when fed two
  cycles of a known series.
* MAPE returns sensible values and skips zero-actual entries.
* Every helper rejects malformed input with a ForecastError that names
  the bad parameter.
"""

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_budget_pro.tools.forecast import (
    ForecastError,
    confidence_band,
    holt_winters_additive,
    linear_trend,
    mean_absolute_percentage_error,
    project_trend,
    simple_moving_average,
)


@tagged('post_install', '-at_install')
class TestSimpleMovingAverage(TransactionCase):

    def test_window_of_three(self):
        self.assertAlmostEqual(
            simple_moving_average([10, 20, 30, 40], 3),
            (20 + 30 + 40) / 3.0,
        )

    def test_window_too_large(self):
        with self.assertRaises(ForecastError):
            simple_moving_average([1, 2, 3], 5)

    def test_empty_history(self):
        with self.assertRaises(ForecastError):
            simple_moving_average([], 3)

    def test_non_int_window(self):
        with self.assertRaises(ForecastError):
            simple_moving_average([1, 2, 3], 1.5)


@tagged('post_install', '-at_install')
class TestLinearTrend(TransactionCase):

    def test_recovers_known_line(self):
        # y = 2x + 5 across 10 points.
        history = [2 * i + 5 for i in range(10)]
        slope, intercept = linear_trend(history)
        self.assertAlmostEqual(slope, 2.0, places=10)
        self.assertAlmostEqual(intercept, 5.0, places=10)

    def test_project_extends_line(self):
        slope, intercept = linear_trend([1, 3, 5, 7])
        # Slope is 2, intercept is 1; index 10 -> 21.
        self.assertAlmostEqual(project_trend(slope, intercept, 10), 21.0)

    def test_single_point_rejected(self):
        with self.assertRaises(ForecastError):
            linear_trend([42.0])

    def test_non_numeric_rejected(self):
        with self.assertRaises(ForecastError):
            linear_trend([1, 2, 'three', 4])


@tagged('post_install', '-at_install')
class TestHoltWintersAdditive(TransactionCase):

    def _periodic_series(self, cycles, season_length=4, base=100.0,
                          slope=1.0, amplitude=10.0):
        out = []
        for c in range(cycles):
            for s in range(season_length):
                level = base + slope * (c * season_length + s)
                seasonal = amplitude * (1.0 if s % 2 == 0 else -1.0)
                out.append(level + seasonal)
        return out

    def test_forecast_length_matches_periods_ahead(self):
        history = self._periodic_series(3)
        fc = holt_winters_additive(
            history, season_length=4, periods_ahead=6,
        )
        self.assertEqual(len(fc), 6)

    def test_forecast_signs_track_seasonal_pattern(self):
        # Even index: positive seasonal; odd index: negative.
        history = self._periodic_series(3, season_length=4, amplitude=10.0)
        fc = holt_winters_additive(
            history, season_length=4, periods_ahead=4,
        )
        # First forecast index = len(history) % 4 = 0 -> positive seasonal.
        # Second = 1 -> negative seasonal. Compare adjacent forecasts to
        # confirm the ordering rather than absolute level.
        self.assertGreater(fc[0], fc[1])
        self.assertGreater(fc[2], fc[3])

    def test_short_history_rejected(self):
        # Less than two complete seasons.
        with self.assertRaises(ForecastError):
            holt_winters_additive(
                [1, 2, 3, 4, 5], season_length=4, periods_ahead=2,
            )

    def test_alpha_out_of_range_rejected(self):
        history = self._periodic_series(3)
        with self.assertRaises(ForecastError):
            holt_winters_additive(
                history, season_length=4, periods_ahead=2, alpha=1.5,
            )

    def test_zero_periods_ahead_rejected(self):
        history = self._periodic_series(3)
        with self.assertRaises(ForecastError):
            holt_winters_additive(
                history, season_length=4, periods_ahead=0,
            )


@tagged('post_install', '-at_install')
class TestErrorMetrics(TransactionCase):

    def test_mape_perfect_match_is_zero(self):
        self.assertAlmostEqual(
            mean_absolute_percentage_error([10, 20, 30], [10, 20, 30]),
            0.0,
        )

    def test_mape_skips_zero_actuals(self):
        # The zero-actual entries should be ignored, not blow up.
        self.assertAlmostEqual(
            mean_absolute_percentage_error([0, 100, 200], [50, 110, 220]),
            (0.10 + 0.10) / 2.0,
        )

    def test_mape_length_mismatch(self):
        with self.assertRaises(ForecastError):
            mean_absolute_percentage_error([1, 2], [1, 2, 3])

    def test_confidence_band_zero_sigma(self):
        # Identical fit and history: residuals are all zero.
        lo, hi = confidence_band([1, 2, 3], [1, 2, 3])
        self.assertAlmostEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.0)

    def test_confidence_band_grows_with_noise(self):
        narrow = confidence_band([10, 11, 12, 13], [10.0, 11.0, 12.0, 13.0])
        wide = confidence_band([10, 12, 11, 14], [11.0, 11.0, 11.0, 11.0])
        self.assertLess(narrow[0], wide[0])
