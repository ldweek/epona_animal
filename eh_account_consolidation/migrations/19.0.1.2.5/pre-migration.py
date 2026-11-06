# -*- encoding: utf-8 -*-
"""Fail closed on every pre-provenance consolidation evidence graph."""


_QUARANTINE_NOTE = (
    "[19.0.1.2.5] Pre-control consolidation evidence retained but "
    "quarantined: no durable current-engine provenance was recorded."
)


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (table,))
    return bool(cr.fetchone()[0])


def _column_exists(cr, table, column):
    cr.execute(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = %s
           AND column_name = %s
        """,
        (table, column),
    )
    return bool(cr.fetchone())


def _columns_exist(cr, table, *columns):
    return _table_exists(cr, table) and all(
        _column_exists(cr, table, column) for column in columns
    )


def migrate(cr, version):
    if not version:
        return

    # Pre-migrations run before the registry adds the new ownership columns.
    # Adding them without defaults leaves existing rows NULL, which is a safe,
    # idempotent marker for precisely the legacy population inspected below.
    cr.execute(
        "ALTER TABLE eh_consol_run "
        "ADD COLUMN IF NOT EXISTS engine_provenance_version integer"
    )
    cr.execute(
        "ALTER TABLE eh_consol_run "
        "ADD COLUMN IF NOT EXISTS legacy_evidence_quarantined boolean"
    )
    cr.execute(
        "ALTER TABLE eh_consol_run "
        "ADD COLUMN IF NOT EXISTS legacy_evidence_quarantine_reason text"
    )

    # Every released 1.0.x / 1.1.x database has a different subset of the
    # fields below.  A pre-migration runs before the new registry shapes the
    # tables, so mention a legacy column only after proving it exists.
    evidence = []
    if _columns_exist(cr, 'eh_consol_run', 'period_from', 'period_to'):
        evidence.append("run.period_from > run.period_to")
    if _column_exists(cr, 'eh_consol_run', 'state'):
        evidence.append("run.state IS DISTINCT FROM 'draft'")
    for optional_column in (
        'move_id', 'computed_at', 'computed_by_id',
        'reviewed_at', 'reviewed_by_id', 'closed_at', 'closed_by_id',
        'reversal_move_id', 'reversed_at', 'reversed_by_id',
    ):
        if _column_exists(cr, 'eh_consol_run', optional_column):
            evidence.append("run.%s IS NOT NULL" % optional_column)
    for numeric_column in (
        'cta_amount', 'nci_amount', 'goodwill_impairment_amount',
    ):
        if _column_exists(cr, 'eh_consol_run', numeric_column):
            evidence.extend((
                "COALESCE(run.%s, 0.0) <> 0.0" % numeric_column,
                "COALESCE(run.%s, 0.0)::text IN "
                "('NaN', 'Infinity', '-Infinity')" % numeric_column,
            ))
    if _column_exists(cr, 'eh_consol_run', 'goodwill_recoverable_amount'):
        evidence.append(
            "COALESCE(run.goodwill_recoverable_amount, 0.0)::text "
            "IN ('NaN', 'Infinity', '-Infinity')"
        )
    if _column_exists(cr, 'eh_consol_run', 'consolidation_warning'):
        evidence.append("run.consolidation_warning IS NOT NULL")
    if _column_exists(cr, 'eh_consol_run', 'presentation_currency_id'):
        evidence.append(
            "EXISTS (SELECT 1 FROM res_currency AS currency "
            "WHERE currency.id = run.presentation_currency_id "
            "AND currency.rounding::text IN "
            "('NaN', 'Infinity', '-Infinity'))"
        )
    if _columns_exist(cr, 'eh_consol_run_line', 'run_id'):
        evidence.append(
            "EXISTS (SELECT 1 FROM eh_consol_run_line AS line "
            "WHERE line.run_id = run.id)"
        )
        if _column_exists(cr, 'eh_consol_run_line', 'amount'):
            evidence.append(
                "EXISTS (SELECT 1 FROM eh_consol_run_line AS line "
                "WHERE line.run_id = run.id AND line.amount::text IN "
                "('NaN', 'Infinity', '-Infinity'))"
            )
    member_numbers = [
        column for column in (
            'ownership_pct', 'investment_amount', 'acquisition_equity',
            'historical_rate', 'nci_fair_value', 'disposal_pct',
        ) if _column_exists(cr, 'eh_consol_member', column)
    ] if _columns_exist(cr, 'eh_consol_member', 'entity_id') else []
    if member_numbers and _column_exists(cr, 'eh_consol_run', 'entity_id'):
        evidence.append(
            "EXISTS (SELECT 1 FROM eh_consol_member AS member "
            "WHERE member.entity_id = run.entity_id AND (%s))"
            % ' OR '.join(
                "member.%s::text IN ('NaN', 'Infinity', '-Infinity')"
                % column for column in member_numbers
            )
        )
    elimination_evidence = [
        "elimination.state IS DISTINCT FROM 'draft'"
        for _unused in (0,)
        if _column_exists(cr, 'eh_consol_elimination', 'state')
    ]
    elimination_evidence.extend(
        "elimination.%s IS NOT NULL" % column
        for column in ('posted_at', 'posted_by_id')
        if _column_exists(cr, 'eh_consol_elimination', column)
    )
    if (
        elimination_evidence
        and _columns_exist(cr, 'eh_consol_elimination', 'run_id')
    ):
        evidence.append(
            "EXISTS (SELECT 1 FROM eh_consol_elimination AS elimination "
            "WHERE elimination.run_id = run.id AND (%s))"
            % ' OR '.join(elimination_evidence)
        )
    finite_elimination_columns = [
        column for column in ('debit', 'credit', 'amount')
        if _column_exists(cr, 'eh_consol_elimination_line', column)
    ]
    if (
        finite_elimination_columns
        and _columns_exist(cr, 'eh_consol_elimination', 'id', 'run_id')
        and _columns_exist(
            cr, 'eh_consol_elimination_line', 'elimination_id',
        )
    ):
        evidence.append(
            "EXISTS (SELECT 1 FROM eh_consol_elimination AS elimination "
            "JOIN eh_consol_elimination_line AS line "
            "ON line.elimination_id = elimination.id "
            "WHERE elimination.run_id = run.id AND (%s))"
            % ' OR '.join(
                "line.%s::text IN ('NaN', 'Infinity', '-Infinity')"
                % column for column in finite_elimination_columns
            )
        )
    if _table_exists(cr, 'eh_consol_run_move_history_rel'):
        evidence.append(
            "EXISTS (SELECT 1 FROM eh_consol_run_move_history_rel AS history "
            "WHERE history.run_id = run.id)"
        )
    if (
        _columns_exist(
            cr, 'eh_consol_member', 'entity_id', 'investment_currency_id',
        )
        and _columns_exist(
            cr, 'eh_consol_entity', 'id', 'presentation_currency_id',
        )
    ):
        cr.execute(
            """
            UPDATE eh_consol_member AS member
               SET investment_currency_id = entity.presentation_currency_id
              FROM eh_consol_entity AS entity
             WHERE entity.id = member.entity_id
            """
        )
    # Keep psycopg placeholders and escaped literal percent signs intact.
    # Python ``%`` interpolation would collapse ``%%`` before ``execute``
    # sees the parameterised query, making psycopg parse the LIKE wildcards
    # as additional placeholders.
    quarantine_sql = (
        """
        UPDATE eh_consol_run AS run
           SET legacy_evidence_quarantined = TRUE,
               legacy_evidence_quarantine_reason = CASE
                   WHEN COALESCE(
                       run.legacy_evidence_quarantine_reason, ''
                   ) LIKE '%%[19.0.1.2.5]%%'
                   THEN run.legacy_evidence_quarantine_reason
                   WHEN COALESCE(
                       BTRIM(run.legacy_evidence_quarantine_reason), ''
                   ) = ''
                   THEN %s
                   ELSE run.legacy_evidence_quarantine_reason
                        || E'\\n' || %s
               END
         WHERE COALESCE(run.engine_provenance_version, 0) = 0
           AND ("""
        + (' OR '.join(evidence) if evidence else 'FALSE')
        + ")"
    )
    cr.execute(
        quarantine_sql,
        (_QUARANTINE_NOTE, _QUARANTINE_NOTE),
    )
    cr.execute(
        """
        UPDATE eh_consol_run
           SET engine_provenance_version = 0,
               legacy_evidence_quarantined = COALESCE(
                   legacy_evidence_quarantined, FALSE
               )
         WHERE engine_provenance_version IS NULL
        """
    )
    cr.execute(
        "ALTER TABLE eh_consol_run "
        "ALTER COLUMN engine_provenance_version SET DEFAULT 0"
    )
    cr.execute(
        "ALTER TABLE eh_consol_run "
        "ALTER COLUMN legacy_evidence_quarantined SET DEFAULT FALSE"
    )

    # Deterministic stored outputs are repaired only where the installed
    # schema already carries both the target and every source column. This
    # never promotes a run: legacy evidence remains quarantined/version zero.
    repair_sets = []
    if (
        _columns_exist(
            cr, 'eh_consol_run', 'entity_id', 'presentation_currency_id',
        )
        and _columns_exist(
            cr, 'eh_consol_entity', 'id', 'presentation_currency_id',
        )
    ):
        repair_sets.append(
            "presentation_currency_id = entity.presentation_currency_id"
        )
    line_basis = _columns_exist(
        cr, 'eh_consol_run_line', 'run_id', 'kind', 'amount',
    )
    for target, kind in (
        ('cta_amount', 'cta'), ('nci_amount', 'nci'),
    ):
        if line_basis and _column_exists(cr, 'eh_consol_run', target):
            repair_sets.append(
                "%s = COALESCE((SELECT SUM(line.amount) "
                "FROM eh_consol_run_line AS line "
                "WHERE line.run_id = run.id AND line.kind = '%s'), 0.0)"
                % (target, kind)
            )
    if (
        line_basis
        and _column_exists(
            cr, 'eh_consol_run', 'goodwill_impairment_amount',
        )
    ):
        repair_sets.append(
            "goodwill_impairment_amount = COALESCE(("
            "SELECT SUM(CASE WHEN line.amount > 0.0 THEN line.amount "
            "ELSE 0.0 END) FROM eh_consol_run_line AS line "
            "WHERE line.run_id = run.id AND line.kind = 'impairment'"
            "), 0.0)"
        )
    if repair_sets and _columns_exist(
        cr, 'eh_consol_run', 'entity_id',
    ) and _column_exists(cr, 'eh_consol_entity', 'id'):
        cr.execute(
            "UPDATE eh_consol_run AS run SET %s "
            "FROM eh_consol_entity AS entity "
            "WHERE entity.id = run.entity_id"
            % ', '.join(repair_sets)
        )
    if (
        _columns_exist(
            cr, 'eh_consol_run_line', 'run_id', 'presentation_currency_id',
        )
        and _columns_exist(
            cr, 'eh_consol_run', 'id', 'presentation_currency_id',
        )
    ):
        cr.execute(
            """
            UPDATE eh_consol_run_line AS line
               SET presentation_currency_id = run.presentation_currency_id
              FROM eh_consol_run AS run
             WHERE run.id = line.run_id
            """
        )
    elimination_sets = []
    if _columns_exist(
        cr, 'eh_consol_elimination_line', 'debit', 'credit', 'amount',
    ):
        elimination_sets.append(
            "amount = COALESCE(line.debit, 0.0) "
            "- COALESCE(line.credit, 0.0)"
        )
    if (
        _column_exists(
            cr, 'eh_consol_elimination_line', 'presentation_currency_id',
        )
        and _column_exists(cr, 'eh_consol_run', 'presentation_currency_id')
    ):
        elimination_sets.append(
            "presentation_currency_id = run.presentation_currency_id"
        )
    if (
        elimination_sets
        and _columns_exist(
            cr, 'eh_consol_elimination_line', 'elimination_id',
        )
        and _columns_exist(
            cr, 'eh_consol_elimination', 'id', 'run_id',
        )
        and _column_exists(cr, 'eh_consol_run', 'id')
    ):
        cr.execute(
            "UPDATE eh_consol_elimination_line AS line SET %s "
            "FROM eh_consol_elimination AS elimination "
            "JOIN eh_consol_run AS run ON run.id = elimination.run_id "
            "WHERE elimination.id = line.elimination_id"
            % ', '.join(elimination_sets)
        )
