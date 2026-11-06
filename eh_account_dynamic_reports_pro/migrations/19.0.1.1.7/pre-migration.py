# -*- coding: utf-8 -*-
"""Bound legacy forecast/webhook work before new SQL constraints load."""

import logging


_logger = logging.getLogger(__name__)


def _columns(cr, table_name):
    cr.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s",
        [table_name],
    )
    return {row[0] for row in cr.fetchall()}


def migrate(cr, version):
    forecast_count = 0
    forecast_columns = _columns(cr, 'eh_report_forecast')
    if 'horizon_months' in forecast_columns:
        cr.execute(
            "UPDATE eh_report_forecast "
            "SET horizon_months = CASE "
            "WHEN horizon_months < 1 THEN 1 ELSE 24 END "
            "WHERE horizon_months < 1 OR horizon_months > 24"
        )
        forecast_count = cr.rowcount

    schedule_count = 0
    schedule_columns = _columns(cr, 'eh_report_schedule')
    if 'webhook_timeout' in schedule_columns:
        # Missing legacy values equal today's safe default. Email-only rows
        # must never be quarantined for an unused webhook setting.
        cr.execute(
            "UPDATE eh_report_schedule SET webhook_timeout = 15 "
            "WHERE webhook_timeout IS NULL"
        )
        timeout_parts = ["webhook_timeout = 15"]
        if {
            'active', 'delivery_channel', 'last_run_status', 'last_error',
        }.issubset(schedule_columns):
            timeout_parts.extend([
                "active = CASE WHEN delivery_channel IN "
                "('webhook', 'both') THEN FALSE ELSE active END",
                "last_run_status = CASE WHEN delivery_channel IN "
                "('webhook', 'both') THEN 'error' ELSE last_run_status END",
                "last_error = CASE WHEN delivery_channel IN "
                "('webhook', 'both') THEN %s ELSE last_error END",
            ])
            params = [
                "Disabled during upgrade: legacy webhook timeout was "
                "outside the safe one-to-60-second range. Review and "
                "reactivate.",
            ]
        else:
            params = []
        cr.execute(
            "UPDATE eh_report_schedule SET %s "
            "WHERE webhook_timeout < 1 OR webhook_timeout > 60"
            % ', '.join(timeout_parts),
            params,
        )
        schedule_count = cr.rowcount

    if forecast_count or schedule_count:
        _logger.warning(
            "Bound %s legacy forecast horizon(s) and normalized %s "
            "unsafe report schedule timeout(s).",
            forecast_count,
            schedule_count,
        )
