# -*- encoding: utf-8 -*-
"""Replace terminal period uniqueness with one-live-revision uniqueness."""


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        DO $$
        DECLARE constraint_name text;
        BEGIN
            FOR constraint_name IN
                SELECT conname
                  FROM pg_constraint
                 WHERE conrelid = 'eh_benefit_valuation'::regclass
                   AND contype = 'u'
                   AND pg_get_constraintdef(oid)
                       LIKE 'UNIQUE (plan_id, period_end)%'
            LOOP
                EXECUTE format(
                    'ALTER TABLE eh_benefit_valuation '
                    'DROP CONSTRAINT %I', constraint_name
                );
            END LOOP;
        END $$
    """)
    cr.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS
            eh_benefit_valuation_live_period_uniq
        ON eh_benefit_valuation (plan_id, period_end)
        WHERE state IN ('draft', 'posted')
    """)
