# -*- coding: utf-8 -*-
"""Normalise Deferred Tax defaults and reporting-date sequence metadata."""


def migrate(cr, version):
    cr.execute("""
        DO $$
        BEGIN
            IF to_regclass('eh_tax_jurisdiction') IS NOT NULL THEN
                WITH ranked AS (
                    SELECT id,
                           row_number() OVER (
                               PARTITION BY company_id ORDER BY id
                           ) AS position
                      FROM eh_tax_jurisdiction
                     WHERE is_company_default IS TRUE
                )
                UPDATE eh_tax_jurisdiction jurisdiction
                   SET is_company_default = FALSE
                  FROM ranked
                 WHERE jurisdiction.id = ranked.id
                   AND ranked.position > 1;

                CREATE UNIQUE INDEX IF NOT EXISTS
                    eh_tax_jurisdiction_one_default_company_uniq
                ON eh_tax_jurisdiction (company_id)
                WHERE is_company_default IS TRUE;
            END IF;
        END $$
    """)
    cr.execute("""
        UPDATE ir_sequence
           SET use_date_range = TRUE
         WHERE code = 'eh.deferred.tax.run'
    """)
