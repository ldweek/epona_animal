# -*- encoding: utf-8 -*-
"""Restore the builder-to-report ownership graph after schema expansion.

This repair is deliberately independent of the optional legacy saved-view
table.  A missing saved-view source must never gate builder ownership repair.
No records are deleted: unusable builder-handler definitions are deactivated
and retained for operator inspection or rollback.
"""

import logging


_logger = logging.getLogger(__name__)

_BUILDER_HANDLER = 'eh.account.dynamic.report.handler.builder'


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
    report_columns = _table_columns(cr, 'eh_account_dynamic_report')

    link_builder = {
        'id', 'code', 'published_report_id',
    } <= builder_columns
    link_report = {'id', 'code', 'handler_model'} <= report_columns

    linked = 0
    if link_builder and link_report:
        # MIN(id) is only a legacy-corruption tie-breaker.  Healthy databases
        # already enforce unique report codes, but deterministic selection
        # keeps this repair stable if that old constraint was absent.
        cr.execute(
            "WITH candidates AS ("
            " SELECT builder.id AS builder_id, MIN(report.id) AS report_id "
            " FROM eh_report_builder AS builder "
            " JOIN eh_account_dynamic_report AS report "
            "   ON report.code = builder.code "
            "  AND report.handler_model = %s "
            " WHERE builder.published_report_id IS NULL "
            " GROUP BY builder.id"
            ") "
            "UPDATE eh_report_builder AS builder "
            "SET published_report_id = candidates.report_id "
            "FROM candidates "
            "WHERE builder.id = candidates.builder_id "
            "AND builder.published_report_id IS NULL",
            (_BUILDER_HANDLER,),
        )
        linked = cr.rowcount

    company_builder = {
        'company_id', 'published_report_id',
    } <= builder_columns
    company_report = {
        'id', 'company_id', 'handler_model',
    } <= report_columns
    companies = 0
    if company_builder and company_report:
        cr.execute(
            "UPDATE eh_account_dynamic_report AS report "
            "SET company_id = builder.company_id "
            "FROM eh_report_builder AS builder "
            "WHERE report.id = builder.published_report_id "
            "AND report.handler_model = %s "
            "AND builder.company_id IS NOT NULL "
            "AND report.company_id IS DISTINCT FROM builder.company_id",
            (_BUILDER_HANDLER,),
        )
        companies = cr.rowcount

    orphan_builder = {'code'} <= builder_columns
    orphan_report = {
        'code', 'handler_model', 'active',
    } <= report_columns
    deactivated = 0
    if orphan_builder and orphan_report:
        cr.execute(
            "UPDATE eh_account_dynamic_report AS report "
            "SET active = FALSE "
            "WHERE report.handler_model = %s "
            "AND report.active IS DISTINCT FROM FALSE "
            "AND NOT EXISTS ("
            " SELECT 1 FROM eh_report_builder AS builder "
            " WHERE builder.code = report.code"
            ")",
            (_BUILDER_HANDLER,),
        )
        deactivated = cr.rowcount

    if not (link_builder and link_report):
        _logger.warning(
            "Builder report linking skipped: required legacy columns are "
            "unavailable."
        )
    if linked or companies or deactivated:
        _logger.warning(
            "Repaired legacy builder publication graph: linked=%d "
            "companies=%d deactivated_orphans=%d.",
            linked,
            companies,
            deactivated,
        )
