# -*- encoding: utf-8 -*-
"""Normalize the newly surfaced portal statement-period setting."""

import logging

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)

_CONFIG_KEY = 'eh_account_portal_extra.max_statement_days'
_DEFAULT_DAYS = 366
_MAX_DAYS = 3660


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    parameters = env['ir.config_parameter'].sudo()
    # ``get_param`` returns boolean ``False`` for a missing key.  Feeding
    # that sentinel to ``int`` yields zero, which the old migration then
    # clamped to one day.  Treat only the missing sentinel as the documented
    # default; every explicitly stored numeric value (including 1) remains
    # authoritative.
    raw_value = parameters.get_param(_CONFIG_KEY)
    try:
        days = (
            _DEFAULT_DAYS
            if raw_value is False or raw_value is None
            else int(raw_value)
        )
    except (TypeError, ValueError):
        days = _DEFAULT_DAYS
    days = min(max(days, 1), _MAX_DAYS)
    normalized_value = str(days)
    if raw_value != normalized_value:
        parameters.set_param(_CONFIG_KEY, normalized_value)
    _logger.info(
        "Portal Extra 19.0.1.0.8 normalized %s to %d day(s).",
        _CONFIG_KEY,
        days,
    )
