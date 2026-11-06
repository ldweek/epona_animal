# -*- encoding: utf-8 -*-
"""Expand legacy Pro saved views into viewer's canonical saved-view table.

Source table stays untouched. That makes rollback possible and lets operators
inspect rows skipped because of malformed JSON, orphaned reports, or a key
already owned by a newer canonical view. Re-running migration is idempotent.
"""

import json
import logging


_logger = logging.getLogger(__name__)


def _reject_non_json_constant(value):
    raise ValueError("non-finite JSON constant: %s" % value)


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
    cr.execute("SELECT to_regclass('eh_report_saved_view')")
    if not cr.fetchone()[0]:
        return

    columns = _table_columns(cr, 'eh_report_saved_view')
    required = {
        'name', 'report_id', 'user_id', 'company_id', 'options_json',
    }
    missing = required - columns
    if missing:
        _logger.error(
            "Legacy saved-view migration skipped: source table misses %s",
            sorted(missing),
        )
        return

    def source(column, fallback):
        return 'legacy.%s' % column if column in columns else fallback

    cr.execute("SELECT COUNT(*) FROM eh_report_saved_view")
    source_count = cr.fetchone()[0]
    cr.execute(
        "SELECT legacy.name, report.code, legacy.user_id, "
        "       legacy.company_id, legacy.options_json, "
        "       {shared}, {notes}, {sequence}, {pinned}, "
        "       {created_on}, {last_used_at}, {use_count}, "
        "       {create_uid}, {create_date}, {write_uid}, {write_date} "
        "FROM eh_report_saved_view AS legacy "
        "JOIN eh_account_dynamic_report AS report "
        "  ON report.id = legacy.report_id "
        "ORDER BY legacy.id".format(
            shared=source('is_shared', 'FALSE'),
            notes=source('notes', 'NULL'),
            sequence=source('sequence', '10'),
            pinned=source('pinned', 'FALSE'),
            created_on=source(
                'created_on', source('create_date', 'NULL'),
            ),
            last_used_at=source('last_used_at', 'NULL'),
            use_count=source('use_count', '0'),
            create_uid=source('create_uid', 'legacy.user_id'),
            create_date=source('create_date', 'NULL'),
            write_uid=source('write_uid', 'legacy.user_id'),
            write_date=source('write_date', 'NULL'),
        )
    )
    rows = cr.fetchall()
    target_columns = _table_columns(
        cr, 'eh_account_report_saved_view',
    )
    period_column = (
        ', period_preset'
        if 'period_preset' in target_columns
        else ''
    )
    period_value = ", 'options'" if period_column else ''
    orphaned = source_count - len(rows)
    migrated = 0
    malformed = 0
    conflicts = 0
    for row in rows:
        raw_options = row[4]
        try:
            options = json.loads(
                raw_options,
                parse_constant=_reject_non_json_constant,
            )
        except (TypeError, ValueError):
            malformed += 1
            continue
        if not isinstance(options, dict):
            malformed += 1
            continue
        cr.execute(
            "INSERT INTO eh_account_report_saved_view "
            " (name, report_code, user_id, company_id, options_json, "
            "  shared, notes, sequence, pinned, created_on, last_used_at, "
            "  use_count, create_uid, create_date, write_uid, write_date"
            "  {period_column}) "
            "SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "       %s, %s, %s, %s, %s{period_value} "
            "WHERE NOT EXISTS ("
            " SELECT 1 FROM eh_account_report_saved_view "
            " WHERE user_id = %s AND report_code = %s AND name = %s"
            ")".format(
                period_column=period_column,
                period_value=period_value,
            ),
            tuple(row) + (row[2], row[1], row[0]),
        )
        if cr.rowcount:
            migrated += 1
        else:
            conflicts += 1

    # Preserve XML-ID continuity for demo/custom module records that pointed
    # at the old model. Rows without a valid canonical target stay unchanged
    # and remain visible in migration telemetry instead of being guessed.
    cr.execute(
        "UPDATE ir_model_data AS external_id "
        "SET model = 'eh.account.report.saved_view', "
        "    res_id = target.id "
        "FROM eh_report_saved_view AS legacy "
        "JOIN eh_account_dynamic_report AS report "
        "  ON report.id = legacy.report_id "
        "JOIN eh_account_report_saved_view AS target "
        "  ON target.user_id = legacy.user_id "
        " AND target.report_code = report.code "
        " AND target.name = legacy.name "
        "WHERE external_id.model = 'eh.report.saved.view' "
        "  AND external_id.res_id = legacy.id"
    )
    remapped_external_ids = cr.rowcount

    if (migrated or malformed or conflicts or orphaned
            or remapped_external_ids):
        _logger.warning(
            "Legacy Pro saved views: migrated=%d malformed=%d conflicts=%d "
            "orphaned=%d external_ids=%d; "
            "source table retained for rollback.",
            migrated, malformed, conflicts, orphaned,
            remapped_external_ids,
        )
