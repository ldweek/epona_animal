# -*- coding: utf-8 -*-
"""Replace period-wide uniqueness with one-live-run uniqueness.

Reversed IAS 12 runs are immutable audit history.  They must not prevent a
corrected run for the same reporting date, while draft/computed/posted runs
must remain unique under concurrent creation.
"""


def migrate(cr, version):
    cr.execute("""
        DO $$
        DECLARE constraint_name text;
        BEGIN
            IF to_regclass('eh_deferred_tax_run') IS NULL THEN
                RETURN;
            END IF;
            FOR constraint_name IN
                SELECT conname
                  FROM pg_constraint
                 WHERE conrelid = 'eh_deferred_tax_run'::regclass
                   AND contype = 'u'
                   AND pg_get_constraintdef(oid)
                       LIKE 'UNIQUE (company_id, period_end)%'
            LOOP
                EXECUTE format(
                    'ALTER TABLE eh_deferred_tax_run DROP CONSTRAINT %I',
                    constraint_name
                );
            END LOOP;
        END $$
    """)
    cr.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS
            eh_deferred_tax_run_live_period_uniq
        ON eh_deferred_tax_run (company_id, period_end)
        WHERE state IN ('draft', 'computed', 'posted')
    """)
