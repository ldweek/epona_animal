# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Shared validation and period presets for persisted report options."""

import json

from odoo import fields


MAX_OPTIONS_JSON_BYTES = 256 * 1024
PERIOD_PRESET_SELECTION = [
    ('options', "Use Saved Options"),
    ('current_month', "Current Month to Today"),
    ('previous_month', "Previous Complete Month"),
    ('current_quarter', "Current Quarter to Today"),
    ('current_fiscal_year', "Current Fiscal Year to Today"),
    ('custom', "Custom Date Range"),
]
_DATE_TOKENS = frozenset({
    'today',
    'auto_month_start',
    'auto_month_end',
    'auto_qtd',
    'auto_ytd',
    'auto_prev_month_start',
    'auto_prev_month_end',
})


def _reject_json_constant(value):
    raise ValueError("non-standard JSON constant %s is not allowed" % value)


def _raise(error_class, message):
    raise error_class(message)


def parse_options_json(raw_options, owner_label, error_class):
    """Return one bounded JSON object with validated date semantics."""
    if not isinstance(raw_options, str) or not raw_options.strip():
        _raise(
            error_class,
            "%s options must be a non-empty JSON object." % owner_label,
        )
    if len(raw_options.encode('utf-8')) > MAX_OPTIONS_JSON_BYTES:
        _raise(
            error_class,
            "%s options exceed the 256 KiB safety limit." % owner_label,
        )
    try:
        options = json.loads(
            raw_options,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError) as exc:
        _raise(
            error_class,
            "%s options must be valid JSON: %s" % (owner_label, exc),
        )
    if not isinstance(options, dict):
        _raise(
            error_class,
            "%s options must be a JSON object." % owner_label,
        )
    validate_options_payload(options, owner_label, error_class)
    return dict(options)


def validate_options_payload(options, owner_label, error_class):
    """Validate persisted date options without rejecting future keys."""
    date_options = options.get('date')
    if date_options is None:
        return True
    if not isinstance(date_options, dict):
        _raise(
            error_class,
            "%s date options must be a JSON object." % owner_label,
        )
    parsed = {}
    for key in ('date_from', 'date_to'):
        value = date_options.get(key)
        if value in (None, False, ''):
            continue
        if not isinstance(value, str):
            _raise(
                error_class,
                "%s %s must be an ISO date or supported period token."
                % (owner_label, key),
            )
        if value in _DATE_TOKENS:
            continue
        if value.startswith('auto_'):
            _raise(
                error_class,
                "%s uses unsupported date token '%s'."
                % (owner_label, value),
            )
        try:
            parsed[key] = fields.Date.to_date(value)
        except (TypeError, ValueError):
            _raise(
                error_class,
                "%s %s must be a valid ISO date." % (owner_label, key),
            )
        if not parsed[key]:
            _raise(
                error_class,
                "%s %s must be a valid ISO date." % (owner_label, key),
            )
    if (
        parsed.get('date_from')
        and parsed.get('date_to')
        and parsed['date_from'] > parsed['date_to']
    ):
        _raise(
            error_class,
            "%s date range must start on or before its end date."
            % owner_label,
        )
    return True


def validate_period_fields(preset, date_from, date_to, owner_label,
                           error_class):
    """Require an ordered explicit range only for Custom Date Range."""
    if preset != 'custom':
        return True
    if not date_from or not date_to:
        _raise(
            error_class,
            "%s custom period requires both Date From and Date To."
            % owner_label,
        )
    if fields.Date.to_date(date_from) > fields.Date.to_date(date_to):
        _raise(
            error_class,
            "%s custom period must start on or before its end date."
            % owner_label,
        )
    return True


def apply_period_preset(options, preset, date_from=False, date_to=False):
    """Overlay one structured period without discarding other filters."""
    if not preset or preset == 'options':
        return dict(options)
    ranges = {
        'current_month': ('auto_month_start', 'today'),
        'previous_month': (
            'auto_prev_month_start', 'auto_prev_month_end',
        ),
        'current_quarter': ('auto_qtd', 'today'),
        'current_fiscal_year': ('auto_ytd', 'today'),
    }
    if preset == 'custom':
        bounds = (
            fields.Date.to_string(date_from),
            fields.Date.to_string(date_to),
        )
    else:
        bounds = ranges[preset]
    scoped = dict(options)
    scoped['date'] = {
        'mode': 'range',
        'date_from': bounds[0],
        'date_to': bounds[1],
    }
    return scoped
