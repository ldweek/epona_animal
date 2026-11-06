# -*- encoding: utf-8 -*-
"""Quarantine every legacy commitment lacking a durable actor/source stamp.

Source fields and amounts were client-writable; old public manual actions also
let ordinary writers reserve arbitrary rows. Exact shape cannot prove either
engine origin or authorised manual approval. Keep every row, remove it from
live availability, and block affected budgets until replacement approval.
"""


_REASON = (
    'Legacy commitments predate durable actor and engine provenance; '
    'all rows were preserved and removed from live availability for review.'
)


def _table_exists(cr, table_name):
    cr.execute("SELECT to_regclass(%s)", (table_name,))
    return bool(cr.fetchone()[0])


def migrate(cr, version):
    if not version:
        return
    if not _table_exists(cr, 'eh_budget_budget'):
        return
    cr.execute(
        "ALTER TABLE eh_budget_budget ADD COLUMN IF NOT EXISTS "
        "eh_commitment_integrity_review_required boolean DEFAULT FALSE"
    )
    cr.execute(
        "ALTER TABLE eh_budget_budget ADD COLUMN IF NOT EXISTS "
        "eh_commitment_integrity_review_reason text"
    )
    if not all(_table_exists(cr, table_name) for table_name in (
        'eh_budget_line', 'eh_budget_commitment',
    )):
        return
    cr.execute("""
        UPDATE eh_budget_budget b
           SET eh_commitment_integrity_review_required = TRUE,
               eh_commitment_integrity_review_reason = %s
         WHERE EXISTS (
             SELECT 1
              FROM eh_budget_line bl
              JOIN eh_budget_commitment c ON c.budget_line_id = bl.id
              WHERE bl.budget_id = b.id
         )
    """, (_REASON,))
    cr.execute("""
        UPDATE eh_budget_commitment
           SET active = FALSE,
               state = 'released',
               eh_integrity_review_reason = COALESCE(
                   NULLIF(eh_integrity_review_reason, ''), %s
               )
    """, (_REASON,))
