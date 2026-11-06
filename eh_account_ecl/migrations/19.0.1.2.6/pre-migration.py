# -*- encoding: utf-8 -*-
"""Replace date-wide uniqueness with one-live-ECL-run uniqueness."""


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        DO $$
        DECLARE constraint_name text;
        BEGIN
            IF to_regclass('eh_ecl_run') IS NULL THEN
                RETURN;
            END IF;
            FOR constraint_name IN
                SELECT conname
                  FROM pg_constraint
                 WHERE conrelid = 'eh_ecl_run'::regclass
                   AND contype = 'u'
                   AND pg_get_constraintdef(oid)
                       LIKE 'UNIQUE (company_id, reporting_date)%'
            LOOP
                EXECUTE format(
                    'ALTER TABLE eh_ecl_run DROP CONSTRAINT %I',
                    constraint_name
                );
            END LOOP;
        END $$
    """)
    cr.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS eh_ecl_run_live_date_uniq
        ON eh_ecl_run (company_id, reporting_date)
        WHERE state IN ('draft', 'computed', 'posted')
    """)
