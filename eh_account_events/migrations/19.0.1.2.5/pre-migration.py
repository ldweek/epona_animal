# -*- encoding: utf-8 -*-
"""Capture legacy stored scope/output drift before ORM recomputation.

Old customer databases can come from any earlier patch level. Every stored
column used by this audit is therefore discovered before its SQL expression
is assembled. Missing optional columns reduce audit coverage for that old
shape; they never make upgrade SQL reference a column that does not exist.
"""

import logging


_logger = logging.getLogger(__name__)


def _table_columns(cr, table):
    cr.execute(
        """
        SELECT column_name
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = %s
        """,
        (table,),
    )
    return {row[0] for row in cr.fetchall()}


def _change_audit_sql(change_columns, line_columns):
    """Build only predicates supported by exact legacy table shape."""
    if 'company_id' not in change_columns:
        return None
    conditions = []
    if 'currency_id' in change_columns:
        conditions.append(
            "change.currency_id IS DISTINCT FROM company.currency_id"
        )
    if {'application', 'change_type'} <= change_columns:
        conditions.append(
            "change.application IS DISTINCT FROM CASE "
            "WHEN change.change_type = 'estimate_change' "
            "THEN 'prospective' ELSE 'retrospective' END"
        )
    if 'retained_earnings_impact' in change_columns:
        conditions.append("change.retained_earnings_impact IS NULL")
        if {'change_id', 'adjustment'} <= line_columns:
            role_filter = (
                " AND line.role = 'affected_item'"
                if 'role' in line_columns else ''
            )
            conditions.append(
                "abs(COALESCE(change.retained_earnings_impact, 0) "
                "- COALESCE((SELECT sum(COALESCE(line.adjustment, 0)) "
                "FROM eh_accounting_change_line AS line "
                "WHERE line.change_id = change.id%s), 0)) "
                "> COALESCE(currency.rounding, 0.01) / 2.0" % role_filter
            )

    child_conditions = []
    if 'company_id' in line_columns:
        child_conditions.append(
            "line.company_id IS DISTINCT FROM change.company_id"
        )
    if 'currency_id' in line_columns and 'currency_id' in change_columns:
        child_conditions.append(
            "line.currency_id IS DISTINCT FROM change.currency_id"
        )
    if {
        'as_restated', 'as_previously_reported', 'adjustment',
    } <= line_columns:
        child_conditions.extend([
            "line.as_restated IS NULL",
            "abs(COALESCE(line.as_restated, 0) "
            "- (COALESCE(line.as_previously_reported, 0) "
            "+ COALESCE(line.adjustment, 0))) "
            "> COALESCE(currency.rounding, 0.01) / 2.0",
        ])
    if 'change_id' in line_columns and child_conditions:
        conditions.append(
            "EXISTS (SELECT 1 FROM eh_accounting_change_line AS line "
            "WHERE line.change_id = change.id AND (%s))"
            % " OR ".join(child_conditions)
        )
    if not conditions:
        return None
    return """
        UPDATE eh_accounting_change AS change
           SET legacy_provenance_quarantined = TRUE,
               legacy_provenance_quarantine_note =
                   '19.0.1.2.5 pre-recompute audit: an available stored '
                   'header or child scope/output did not reconcile. '
                   'Historical values were preserved and ledger transitions '
                   'are blocked pending review.'
          FROM res_company AS company
          JOIN res_currency AS currency
            ON currency.id = company.currency_id
         WHERE company.id = change.company_id
           AND (%s)
    """ % " OR ".join(conditions)


def _event_audit_sql(event_columns):
    """Build IAS 10 drift predicates only from present legacy columns."""
    if 'company_id' not in event_columns:
        return None
    conditions = []
    if 'currency_id' in event_columns:
        conditions.append(
            "event.currency_id IS DISTINCT FROM company.currency_id"
        )
    if {
        'next_period', 'authorized_for_issue_date', 'event_date',
    } <= event_columns:
        conditions.append(
            "event.next_period IS DISTINCT FROM "
            "(event.authorized_for_issue_date IS NOT NULL "
            "AND event.event_date IS NOT NULL "
            "AND event.event_date > event.authorized_for_issue_date)"
        )
    if {'treatment', 'is_adjusting'} <= event_columns:
        conditions.append(
            "event.treatment IS DISTINCT FROM CASE "
            "WHEN event.is_adjusting THEN 'adjust' ELSE 'disclose' END"
        )
    if not conditions:
        return None
    return """
        UPDATE eh_subsequent_event AS event
           SET legacy_provenance_quarantined = TRUE,
               legacy_provenance_quarantine_note =
                   '19.0.1.2.5 pre-recompute audit: an available stored '
                   'scope/output did not reconcile with the event company. '
                   'Historical values were preserved and ledger transitions '
                   'are blocked pending review.'
          FROM res_company AS company
         WHERE company.id = event.company_id
           AND (%s)
    """ % " OR ".join(conditions)


def migrate(cr, version):
    if not version:
        return
    change_columns = _table_columns(cr, 'eh_accounting_change')
    line_columns = _table_columns(cr, 'eh_accounting_change_line')
    event_columns = _table_columns(cr, 'eh_subsequent_event')
    move_columns = _table_columns(cr, 'account_move')

    for table, columns in (
        ('eh_accounting_change', change_columns),
        ('eh_subsequent_event', event_columns),
    ):
        if not columns:
            continue
        cr.execute(
            "ALTER TABLE %s ADD COLUMN IF NOT EXISTS "
            "legacy_provenance_quarantined boolean DEFAULT false" % table
        )
        cr.execute(
            "ALTER TABLE %s ADD COLUMN IF NOT EXISTS "
            "legacy_provenance_quarantine_note text" % table
        )
    if move_columns:
        cr.execute(
            "ALTER TABLE account_move ADD COLUMN IF NOT EXISTS "
            "eh_event_provenance_version integer DEFAULT 0"
        )

    change_sql = _change_audit_sql(change_columns, line_columns)
    if change_sql:
        cr.execute(change_sql)
        change_count = cr.rowcount
    else:
        change_count = 0

    event_sql = _event_audit_sql(event_columns)
    if event_sql:
        cr.execute(event_sql)
        event_count = cr.rowcount
    else:
        event_count = 0
    if change_count or event_count:
        _logger.warning(
            "Events 19.0.1.2.5 pre-recompute audit quarantined %d "
            "accounting change(s) and %d subsequent event(s) with stored "
            "scope/output drift.",
            change_count, event_count,
        )
