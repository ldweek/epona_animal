# -*- encoding: utf-8 -*-
"""Preserve cap intent and reversal dates before 1.0.9 fields load."""


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
        ADD COLUMN IF NOT EXISTS apply_actual_borrowing_cost_cap boolean
        DEFAULT FALSE
    """)
    # Before 1.0.9, a non-zero actual cost unambiguously meant that the cap
    # applied. Zero was indistinguishable from an untouched Float default, so
    # it remains unset for legacy rows; future explicit writes carry the new
    # marker even when their value is exactly zero.
    cr.execute("""
        UPDATE eh_borrowing_cost
           SET apply_actual_borrowing_cost_cap = TRUE
         WHERE actual_borrowing_cost <> 0.0
    """)
    cr.execute("""
        UPDATE eh_borrowing_cost
           SET apply_actual_borrowing_cost_cap = FALSE
         WHERE apply_actual_borrowing_cost_cap IS NULL
    """)
    cr.execute("""
        ALTER TABLE eh_borrowing_cost
        ALTER COLUMN apply_actual_borrowing_cost_cap SET DEFAULT FALSE,
        ALTER COLUMN apply_actual_borrowing_cost_cap SET NOT NULL
    """)
    cr.execute("""
        ALTER TABLE eh_borrowing_cost
        ADD COLUMN IF NOT EXISTS posted_reversal_date date
    """)
    cr.execute("""
        UPDATE eh_borrowing_cost AS cost
           SET posted_reversal_date = reversal.date
          FROM account_move AS reversal
         WHERE reversal.id = cost.reversal_move_id
           AND cost.posted_reversal_date IS NULL
    """)
    # Retire the old global counter. Runtime creation now always acquires the
    # exact company-owned sequence before consuming a number.
    cr.execute("""
        UPDATE ir_sequence
           SET active = FALSE
         WHERE code = 'eh.borrowing.cost'
           AND company_id IS NULL
    """)
