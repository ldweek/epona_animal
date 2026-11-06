# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Revenue forecasting helpers.

Plain Python (no Odoo, no numpy) so the algorithms are unit-testable in
seconds and the maths is auditable line by line. The helpers in this
module take a sequence of historical period totals (typically 12, 24, or
36 months of posted income or expense) and project a forward window.

Three algorithms ship:

* `simple_moving_average(history, window)` -- baseline; the expected
  forward value is the mean of the last `window` periods. Robust to
  noise, blind to trend and seasonality.

* `linear_trend(history)` -- ordinary least squares fit on the index
  versus value pairs. Returns slope and intercept so a caller can
  project any future index. Use when the prior series shows a clear
  monotonic trend with no obvious seasonality.

* `holt_winters_additive(history, season_length, periods_ahead)` --
  triple exponential smoothing with additive seasonality. Fits level,
  trend, seasonal indices from the history and emits a sequence of
  forward values. Recommended default for monthly revenue series with
  any annual cyclicality.

Inputs are validated up front and reject silently-bad shapes (empty
series, mismatched season length) with explicit ForecastError messages
that name the bad parameter. No silent fallback to mean-of-history.
"""

import math


class ForecastError(ValueError):
    """Raised when a forecasting input is malformed or insufficient."""


def _ensure_numeric_series(series, name='series'):
    """Coerce to a list of floats; raise on non-numeric or empty input."""
    if series is None:
        raise ForecastError(f"{name} must not be None")
    out = []
    for i, v in enumerate(series):
        if v is None:
            raise ForecastError(
                f"{name}[{i}] is None; need a numeric value"
            )
        try:
            out.append(float(v))
        except (TypeError, ValueError) as exc:
            raise ForecastError(
                f"{name}[{i}] is not numeric: {v!r} ({exc})"
            ) from None
    if not out:
        raise ForecastError(f"{name} must not be empty")
    return out


def simple_moving_average(history, window):
    """Return the moving average over the last `window` items.

    Useful as a quick baseline. Raises if window > len(history) so the
    caller does not silently get the global mean masquerading as a
    moving average.
    """
    series = _ensure_numeric_series(history, 'history')
    if not isinstance(window, int) or window <= 0:
        raise ForecastError(f"window must be a positive int (got {window!r})")
    if window > len(series):
        raise ForecastError(
            f"window ({window}) exceeds history length ({len(series)})"
        )
    tail = series[-window:]
    return sum(tail) / window


def linear_trend(history):
    """Fit an OLS line through history and return (slope, intercept).

    The independent variable is the integer index 0..n-1 in the order
    given. Use ``project(slope, intercept, index)`` to evaluate the line
    at a future point.

    Raises when history has fewer than two points (a single point does
    not define a slope).
    """
    series = _ensure_numeric_series(history, 'history')
    n = len(series)
    if n < 2:
        raise ForecastError(
            "linear_trend needs at least two history points"
        )
    sum_x = (n - 1) * n / 2.0
    sum_y = sum(series)
    sum_xy = sum(i * v for i, v in enumerate(series))
    sum_x2 = sum(i * i for i in range(n))
    denom = n * sum_x2 - sum_x * sum_x
    if not denom:
        # All x equal: impossible here since indices are 0..n-1, but
        # guard the divide for clarity.
        raise ForecastError("trend slope is undefined for this input")
    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


def project_trend(slope, intercept, index):
    """Evaluate the OLS line at the given index. Returns a float."""
    return slope * index + intercept


def holt_winters_additive(
    history, season_length, periods_ahead,
    alpha=0.3, beta=0.1, gamma=0.1,
):
    """Additive triple exponential smoothing.

    Implements the classic Holt-Winters additive form: a level and
    trend track the running mean and slope of deseasonalised data; a
    seasonal component carries `season_length` indices that recycle
    every cycle. The defaults (alpha=0.3, beta=0.1, gamma=0.1) are the
    industry textbook starting point; callers tuning to their own
    series can override them. For monthly revenue with annual
    seasonality, use season_length=12.

    Returns a list of `periods_ahead` projected values, in time order.

    Raises:
    * ForecastError if any smoothing parameter is outside (0, 1].
    * ForecastError if history is shorter than two seasons (you cannot
      compute a meaningful seasonal index from one cycle of data).
    * ForecastError if periods_ahead is non-positive.
    """
    series = _ensure_numeric_series(history, 'history')
    for name, value in (('alpha', alpha), ('beta', beta), ('gamma', gamma)):
        if not isinstance(value, (int, float)) or not (0.0 < value <= 1.0):
            raise ForecastError(
                f"{name} must be in (0, 1] (got {value!r})"
            )
    if not isinstance(season_length, int) or season_length <= 0:
        raise ForecastError(
            f"season_length must be a positive int (got {season_length!r})"
        )
    if not isinstance(periods_ahead, int) or periods_ahead <= 0:
        raise ForecastError(
            f"periods_ahead must be a positive int (got {periods_ahead!r})"
        )
    if len(series) < 2 * season_length:
        raise ForecastError(
            "history must cover at least two complete seasons; got "
            f"{len(series)} points with season_length={season_length}"
        )

    # Initial level: mean of the first season.
    initial_level = sum(series[:season_length]) / season_length
    # Initial trend: average across paired seasons.
    initial_trend = sum(
        (series[season_length + i] - series[i]) / season_length
        for i in range(season_length)
    ) / season_length
    # Initial seasonal indices: deviation of each first-season point
    # from the initial level.
    seasonal = [series[i] - initial_level for i in range(season_length)]

    level = initial_level
    trend = initial_trend
    fitted = []
    for t, value in enumerate(series):
        season_idx = t % season_length
        prev_level = level
        level = (
            alpha * (value - seasonal[season_idx])
            + (1.0 - alpha) * (prev_level + trend)
        )
        trend = beta * (level - prev_level) + (1.0 - beta) * trend
        seasonal[season_idx] = (
            gamma * (value - level)
            + (1.0 - gamma) * seasonal[season_idx]
        )
        fitted.append(level + trend + seasonal[season_idx])

    forecasts = []
    for k in range(1, periods_ahead + 1):
        season_idx = (len(series) + k - 1) % season_length
        forecasts.append(level + k * trend + seasonal[season_idx])
    return forecasts


def mean_absolute_percentage_error(actual, fitted):
    """Compute MAPE as a fraction (0.05 = 5%) between two equal-length
    series. Skips entries where actual is zero (division would explode).
    Returns 0.0 when no comparable entries remain.
    """
    a = _ensure_numeric_series(actual, 'actual')
    f = _ensure_numeric_series(fitted, 'fitted')
    if len(a) != len(f):
        raise ForecastError(
            f"length mismatch: actual={len(a)}, fitted={len(f)}"
        )
    n = 0
    total = 0.0
    for av, fv in zip(a, f):
        if av == 0.0:
            continue
        total += abs(av - fv) / abs(av)
        n += 1
    if not n:
        return 0.0
    return total / n


def confidence_band(history, fitted, sigma_multiple=1.96):
    """Return (lower_offset, upper_offset) suitable for a 95% band on
    the fitted projection.

    The offsets are computed from the standard deviation of residuals
    (history minus fitted-on-history portion) multiplied by
    `sigma_multiple` (default 1.96 for ~95%). Use them as +/- around
    each forecast point to render a confidence band on a chart.

    Returns (0.0, 0.0) when the residuals are degenerate (zero sigma).
    """
    a = _ensure_numeric_series(history, 'history')
    f = _ensure_numeric_series(fitted, 'fitted')
    n = min(len(a), len(f))
    if n < 2:
        return 0.0, 0.0
    residuals = [a[i] - f[i] for i in range(n)]
    mean = sum(residuals) / n
    var = sum((r - mean) ** 2 for r in residuals) / (n - 1)
    sigma = math.sqrt(var)
    offset = sigma_multiple * sigma
    return offset, offset
