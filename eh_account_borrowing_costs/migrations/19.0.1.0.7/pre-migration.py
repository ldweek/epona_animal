# -*- encoding: utf-8 -*-
"""Preserve legacy period-rate semantics before annual-rate default lands."""


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        SELECT 1
          FROM information_schema.tables
         WHERE table_schema = current_schema()
           AND table_name = 'eh_borrowing_cost'
    """)
    if not cr.fetchone():
        return
    cr.execute("""
        ALTER TABLE eh_borrowing_cost
        ADD COLUMN IF NOT EXISTS capitalisation_rate_basis varchar
    """)
    cr.execute("""
        UPDATE eh_borrowing_cost
           SET capitalisation_rate_basis = 'period'
         WHERE capitalisation_rate_basis IS NULL
            OR capitalisation_rate_basis NOT IN ('annual', 'period')
    """)
    cr.execute("""
        ALTER TABLE eh_borrowing_cost
        ALTER COLUMN capitalisation_rate_basis SET DEFAULT 'annual',
        ALTER COLUMN capitalisation_rate_basis SET NOT NULL
    """)
