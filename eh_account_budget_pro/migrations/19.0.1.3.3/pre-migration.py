# -*- encoding: utf-8 -*-
"""Quarantine legacy commitments whose stored scope was forgeable.

Before server ownership, RPC writes could make stored related budget,
company, or currency disagree with canonical budget line. Record rules and
availability computations trusted those columns. Preserve every row but
remove ambiguous rows from live availability before ORM recomputation can
erase evidence of mismatch.
"""


def _table_exists(cr, table_name):
    cr.execute("SELECT to_regclass(%s)", (table_name,))
    return bool(cr.fetchone()[0])


def migrate(cr, version):
    if not version:
        return
    if not all(_table_exists(cr, table_name) for table_name in (
        'eh_budget_commitment', 'eh_budget_line', 'eh_budget_budget',
    )):
        return
    cr.execute(
        "ALTER TABLE eh_budget_commitment "
        "ADD COLUMN IF NOT EXISTS active boolean DEFAULT TRUE"
    )
    cr.execute(
        "ALTER TABLE eh_budget_commitment "
        "ADD COLUMN IF NOT EXISTS eh_integrity_review_reason text"
    )
    cr.execute("""
        UPDATE eh_budget_commitment c
           SET active = FALSE,
               state = 'released',
               eh_integrity_review_reason =
                   'legacy-related-budget-company-currency-mismatch-'
                   'preserved-for-review'
          FROM eh_budget_line bl
          JOIN eh_budget_budget b ON b.id = bl.budget_id
         WHERE c.budget_line_id = bl.id
           AND (
               c.budget_id IS DISTINCT FROM bl.budget_id
               OR c.company_id IS DISTINCT FROM b.company_id
               OR c.currency_id IS DISTINCT FROM b.currency_id
           )
    """)
    cr.execute("""
        UPDATE eh_budget_commitment c
           SET active = FALSE,
               state = 'released',
               eh_integrity_review_reason =
                   'legacy-missing-budget-line-preserved-for-review'
         WHERE NOT EXISTS (
             SELECT 1 FROM eh_budget_line bl
              WHERE bl.id = c.budget_line_id
         )
    """)
