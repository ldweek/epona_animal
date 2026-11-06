# -*- encoding: utf-8 -*-
"""Normalize legacy builder ownership and required line selections.

Every update is NULL-only and retry-safe.  Existing non-NULL ownership and
line choices are preserved exactly.
"""

import logging


_logger = logging.getLogger(__name__)


def _table_columns(cr, table):
    cr.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = ANY (current_schemas(FALSE)) "
        "AND table_name = %s",
        (table,),
    )
    return {row[0] for row in cr.fetchall()}


def migrate(cr, version):
    if not version:
        return

    builder_columns = _table_columns(cr, 'eh_report_builder')
    user_columns = _table_columns(cr, 'res_users')
    company_columns = _table_columns(cr, 'res_company')
    repaired_builders = 0
    if 'company_id' in builder_columns and 'id' in company_columns:
        if (
            'create_uid' in builder_columns
            and {'id', 'company_id'} <= user_columns
        ):
            creator_company = (
                "(SELECT creator.company_id "
                "FROM res_users AS creator "
                "JOIN res_company AS creator_company "
                "  ON creator_company.id = creator.company_id "
                "WHERE creator.id = builder.create_uid)"
            )
        else:
            creator_company = "NULL"
        cr.execute(
            "UPDATE eh_report_builder AS builder "
            "SET company_id = COALESCE(" + creator_company + ", "
            "    fallback.company_id) "
            "FROM (SELECT MIN(id) AS company_id FROM res_company) "
            "     AS fallback "
            "WHERE builder.company_id IS NULL "
            "AND fallback.company_id IS NOT NULL"
        )
        repaired_builders = cr.rowcount
    elif builder_columns:
        _logger.error(
            "Builder company migration skipped: required company schema "
            "is unavailable."
        )

    line_columns = _table_columns(cr, 'eh_report_builder_line')
    assignments = []
    predicates = []
    if 'account_scope' in line_columns:
        assignments.append("account_scope = COALESCE(account_scope, 'codes')")
        predicates.append("account_scope IS NULL")
    if 'sign' in line_columns:
        assignments.append("sign = COALESCE(sign, '+')")
        predicates.append("sign IS NULL")

    repaired_lines = 0
    if assignments:
        cr.execute(
            "UPDATE eh_report_builder_line SET %s WHERE %s"
            % (', '.join(assignments), ' OR '.join(predicates))
        )
        repaired_lines = cr.rowcount

    forecast_columns = _table_columns(cr, 'eh_report_forecast')
    normalized_forecasts = 0
    if 'monthly_growth_pct' in forecast_columns:
        cr.execute(
            "UPDATE eh_report_forecast "
            "SET monthly_growth_pct = CASE "
            " WHEN monthly_growth_pct IS NULL "
            "      OR monthly_growth_pct::text = 'NaN' THEN 0.0 "
            " WHEN monthly_growth_pct::text = 'Infinity' "
            "      OR monthly_growth_pct > 1000.0 THEN 1000.0 "
            " WHEN monthly_growth_pct::text = '-Infinity' "
            "      OR monthly_growth_pct < -100.0 THEN -100.0 "
            " ELSE monthly_growth_pct END "
            "WHERE monthly_growth_pct IS NULL "
            "OR monthly_growth_pct::text IN ('NaN', 'Infinity', '-Infinity') "
            "OR monthly_growth_pct < -100.0 "
            "OR monthly_growth_pct > 1000.0"
        )
        normalized_forecasts = cr.rowcount

    if repaired_builders or repaired_lines or normalized_forecasts:
        _logger.warning(
            "Normalized legacy report data: builder_companies=%d "
            "builder_lines=%d forecast_growth=%d.",
            repaired_builders,
            repaired_lines,
            normalized_forecasts,
        )
