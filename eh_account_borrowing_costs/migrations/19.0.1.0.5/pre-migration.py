# -*- encoding: utf-8 -*-
"""Quarantine legacy IAS 23 evidence before related-field recomputation."""


_REASON = (
    'Legacy borrowing-cost journal or child evidence predates durable engine '
    'provenance; all rows were preserved for controlled review.'
)


def migrate(cr, version):
    if not version:
        return
    # Pre-migration runs before ORM schema synchronisation.  Databases coming
    # directly from a version predating the reversal workflow do not yet have
    # this column, but the fail-closed audit below must still cover them.
    cr.execute(
        "ALTER TABLE eh_borrowing_cost ADD COLUMN IF NOT EXISTS "
        "reversal_move_id integer"
    )
    cr.execute(
        "ALTER TABLE eh_borrowing_cost ADD COLUMN IF NOT EXISTS "
        "eh_integrity_review_required boolean DEFAULT FALSE"
    )
    cr.execute(
        "ALTER TABLE eh_borrowing_cost ADD COLUMN IF NOT EXISTS "
        "eh_integrity_review_reason text"
    )
    cr.execute("""
        UPDATE eh_borrowing_cost c
           SET eh_integrity_review_required = TRUE,
               eh_integrity_review_reason = COALESCE(
                   NULLIF(c.eh_integrity_review_reason, ''), %s
               )
         WHERE c.move_id IS NOT NULL
            OR c.reversal_move_id IS NOT NULL
            OR EXISTS (
                SELECT 1 FROM eh_borrowing_cost_line l
                 WHERE l.borrowing_cost_id = c.id
                   AND (
                       l.company_id IS DISTINCT FROM c.company_id
                       OR l.currency_id IS DISTINCT FROM c.currency_id
                   )
            )
            OR EXISTS (
                SELECT 1 FROM eh_borrowing_cost_suspension s
                 WHERE s.borrowing_cost_id = c.id
                   AND s.company_id IS DISTINCT FROM c.company_id
            )
    """, (_REASON,))
