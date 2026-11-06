# -*- encoding: utf-8 -*-
"""Allow a corrected FX close while retaining terminal audit history."""


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        DO $$
        DECLARE constraint_name text;
        BEGIN
            IF to_regclass('eh_fx_revaluation_run') IS NULL THEN
                RETURN;
            END IF;
            FOR constraint_name IN
                SELECT conname
                  FROM pg_constraint
                 WHERE conrelid = 'eh_fx_revaluation_run'::regclass
                   AND contype = 'u'
                   AND pg_get_constraintdef(oid)
                       LIKE 'UNIQUE (revaluation_date, company_id)%'
            LOOP
                EXECUTE format(
                    'ALTER TABLE eh_fx_revaluation_run '
                    'DROP CONSTRAINT %I', constraint_name
                );
            END LOOP;
        END $$
    """)
    cr.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS
            eh_fx_revaluation_run_live_date_uniq
        ON eh_fx_revaluation_run (company_id, revaluation_date)
        WHERE state IN ('draft', 'computed', 'posted')
    """)
