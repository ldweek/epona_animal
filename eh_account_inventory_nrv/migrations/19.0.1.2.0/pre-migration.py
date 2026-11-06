# -*- encoding: utf-8 -*-
"""Replace the one-date dead end with immutable, auditable revisions."""

from psycopg2 import sql


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        "ALTER TABLE eh_nrv_run ADD COLUMN IF NOT EXISTS "
        "revision integer DEFAULT 1 NOT NULL"
    )
    cr.execute(
        "ALTER TABLE eh_nrv_run ADD COLUMN IF NOT EXISTS "
        "supersedes_run_id integer"
    )
    # Drop only the legacy UNIQUE(company_id, reporting_date), regardless of
    # the ORM-generated constraint name.  Other unique controls are untouched.
    cr.execute("""
        SELECT con.conname
          FROM pg_constraint con
          JOIN LATERAL (
               SELECT array_agg(att.attname ORDER BY key.ordinality) AS columns
                 FROM unnest(con.conkey) WITH ORDINALITY AS key(attnum, ordinality)
                 JOIN pg_attribute att
                   ON att.attrelid = con.conrelid
                  AND att.attnum = key.attnum
          ) keys ON TRUE
         WHERE con.conrelid = 'eh_nrv_run'::regclass
           AND con.contype = 'u'
           AND keys.columns = ARRAY['company_id', 'reporting_date']::name[]
    """)
    for (constraint_name,) in cr.fetchall():
        cr.execute(sql.SQL(
            "ALTER TABLE eh_nrv_run DROP CONSTRAINT {}"
        ).format(sql.Identifier(constraint_name)))
    cr.execute("""
        SELECT 1
          FROM pg_constraint
         WHERE conrelid = 'eh_nrv_run'::regclass
           AND conname = 'eh_nrv_run_unique_company_date_revision'
    """)
    if not cr.fetchone():
        cr.execute("""
            ALTER TABLE eh_nrv_run
            ADD CONSTRAINT eh_nrv_run_unique_company_date_revision
            UNIQUE (company_id, reporting_date, revision)
        """)
