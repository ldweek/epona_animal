# -*- encoding: utf-8 -*-
"""Upgrade raw chain ids and period identity without losing signed evidence."""

from psycopg2 import sql


def _table_exists(cr, table):
    cr.execute("""
        SELECT 1
          FROM information_schema.tables
         WHERE table_schema = current_schema()
           AND table_name = %s
    """, (table,))
    return bool(cr.fetchone())


def _reference_column(cr, column, model, target_table):
    cr.execute("""
        SELECT data_type
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'eh_audit_pack'
           AND column_name = %s
    """, (column,))
    row = cr.fetchone()
    if not row:
        return
    if row[0] not in ('character varying', 'text'):
        cr.execute(sql.SQL("""
            ALTER TABLE eh_audit_pack
            ALTER COLUMN {} TYPE varchar
            USING CASE
                WHEN {} IS NULL OR {} = 0 THEN NULL
                ELSE %s || ',' || {}::text
            END
        """).format(*[sql.Identifier(column)] * 4), (model,))
    if not _table_exists(cr, target_table):
        # A raw id entered while the optional module was absent never proved a
        # target. Do not turn it into a Reference whose model is not in the
        # registry; clear it fail-safely during this one-time conversion.
        cr.execute(sql.SQL(
            "UPDATE eh_audit_pack SET {} = NULL"
        ).format(sql.Identifier(column)))


def migrate(cr, version):
    if not version or not _table_exists(cr, 'eh_audit_pack'):
        return

    _reference_column(
        cr, 'close_run_ref', 'eh.close.run', 'eh_close_run',
    )
    _reference_column(
        cr, 'year_end_run_ref', 'eh.year.end.run', 'eh_year_end_run',
    )

    cr.execute("""
        ALTER TABLE eh_audit_pack
        ADD COLUMN IF NOT EXISTS revision integer DEFAULT 1
    """)
    cr.execute("""
        UPDATE eh_audit_pack
           SET revision = 1
         WHERE revision IS NULL OR revision < 1
    """)
    cr.execute("""
        ALTER TABLE eh_audit_pack
        ALTER COLUMN revision SET DEFAULT 1,
        ALTER COLUMN revision SET NOT NULL
    """)
    cr.execute("""
        UPDATE eh_audit_pack
           SET advance_lock = TRUE
         WHERE advance_lock IS DISTINCT FROM TRUE
    """)

    # The previous two-column unique key prevents a second immutable revision.
    cr.execute("""
        SELECT conname
          FROM pg_constraint
         WHERE conrelid = 'eh_audit_pack'::regclass
           AND contype = 'u'
           AND pg_get_constraintdef(oid) ILIKE 'UNIQUE (company_id, period_to)%'
           AND pg_get_constraintdef(oid) NOT ILIKE '%revision%'
    """)
    for (constraint_name,) in cr.fetchall():
        cr.execute(sql.SQL(
            "ALTER TABLE eh_audit_pack DROP CONSTRAINT {}"
        ).format(sql.Identifier(constraint_name)))

    # Runtime numbering is now company-owned and uses the period end as its
    # sequence date. Retain the former global row only as an inactive template.
    cr.execute("""
        UPDATE ir_sequence
           SET active = FALSE,
               use_date_range = TRUE
         WHERE code = 'eh.audit.pack'
           AND company_id IS NULL
    """)
