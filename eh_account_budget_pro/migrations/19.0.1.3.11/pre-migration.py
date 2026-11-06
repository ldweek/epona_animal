# -*- encoding: utf-8 -*-
"""Backfill stable version ordinals and purchase-governance scope."""


def _table_exists(cr, table_name):
    cr.execute("SELECT to_regclass(%s)", (table_name,))
    return bool(cr.fetchone()[0])


def _columns_exist(cr, table_name, column_names):
    cr.execute(
        "SELECT attname FROM pg_attribute "
        "WHERE attrelid = to_regclass(%s) "
        "AND attnum > 0 AND NOT attisdropped",
        (table_name,),
    )
    existing = {row[0] for row in cr.fetchall()}
    return set(column_names).issubset(existing)


def _backfill_version_identity(cr):
    if not _table_exists(cr, 'eh_budget_budget'):
        return
    cr.execute(
        "ALTER TABLE eh_budget_budget "
        "ADD COLUMN IF NOT EXISTS version_root_code varchar"
    )
    cr.execute(
        "ALTER TABLE eh_budget_budget "
        "ADD COLUMN IF NOT EXISTS version_sequence integer"
    )
    cr.execute(
        "ALTER TABLE eh_budget_budget "
        "ADD COLUMN IF NOT EXISTS version_label varchar"
    )
    if not _columns_exist(
        cr, 'eh_budget_budget', ('id', 'parent_id', 'code'),
    ):
        return
    cr.execute("""
        WITH RECURSIVE lineage AS (
            SELECT budget.id,
                   budget.id AS root_id,
                   budget.code AS root_code,
                   0 AS depth,
                   ARRAY[budget.id] AS path
              FROM eh_budget_budget AS budget
             WHERE budget.parent_id IS NULL
            UNION ALL
            SELECT child.id,
                   lineage.root_id,
                   lineage.root_code,
                   lineage.depth + 1,
                   lineage.path || child.id
              FROM lineage
              JOIN eh_budget_budget AS child
                ON child.parent_id = lineage.id
             WHERE NOT child.id = ANY(lineage.path)
        ), ranked AS (
            SELECT id,
                   root_code,
                   row_number() OVER (
                       PARTITION BY root_id
                       ORDER BY depth, path, id
                   )::integer AS sequence
              FROM lineage
        )
        UPDATE eh_budget_budget AS budget
           SET version_root_code = ranked.root_code,
               version_sequence = ranked.sequence,
               version_label = 'v' || ranked.sequence::text
          FROM ranked
         WHERE budget.id = ranked.id
    """)
    # Cyclic or otherwise unreachable legacy rows have no provable root.
    # Preserve their unique code as an independent root instead of inventing
    # a relationship; previous lineage quarantine remains authoritative.
    cr.execute("""
        UPDATE eh_budget_budget
           SET version_root_code = COALESCE(version_root_code, code),
               version_sequence = COALESCE(version_sequence, 1),
               version_label = COALESCE(NULLIF(version_label, ''), 'v1')
         WHERE version_root_code IS NULL
            OR version_sequence IS NULL
            OR version_label IS NULL
            OR version_label = ''
    """)
    cr.execute(
        "ALTER TABLE eh_budget_budget "
        "ALTER COLUMN version_root_code SET NOT NULL"
    )
    cr.execute(
        "ALTER TABLE eh_budget_budget "
        "ALTER COLUMN version_sequence SET NOT NULL"
    )
    cr.execute(
        "ALTER TABLE eh_budget_budget "
        "ALTER COLUMN version_label SET NOT NULL"
    )


def _backfill_governance_discriminator(cr):
    if not _table_exists(cr, 'res_company'):
        return
    cr.execute(
        "ALTER TABLE res_company ADD COLUMN IF NOT EXISTS "
        "eh_budget_governance_active boolean DEFAULT FALSE"
    )
    if not _table_exists(cr, 'eh_budget_budget') or not _columns_exist(
        cr,
        'eh_budget_budget',
        ('company_id', 'state', 'overrun_policy'),
    ):
        return
    cr.execute("""
        UPDATE res_company AS company
           SET eh_budget_governance_active = EXISTS (
               SELECT 1
                 FROM eh_budget_budget AS budget
                WHERE budget.company_id = company.id
                  AND budget.state IN ('confirmed', 'revised')
                  AND budget.overrun_policy != 'off'
           )
    """)


def migrate(cr, version):
    if not version:
        return
    _backfill_version_identity(cr)
    _backfill_governance_discriminator(cr)
