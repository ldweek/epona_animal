# -*- encoding: utf-8 -*-
"""Quarantine every legacy inverse journal link without blessing its shape."""


_REASON = (
    'Legacy borrowing-cost journal or child evidence predates durable engine '
    'provenance; all rows were preserved for controlled review.'
)


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        UPDATE eh_borrowing_cost c
           SET eh_integrity_review_required = TRUE,
               eh_integrity_review_reason = COALESCE(
                   NULLIF(c.eh_integrity_review_reason, ''), %s
               )
         WHERE c.move_id IS NOT NULL
            OR c.reversal_move_id IS NOT NULL
            OR EXISTS (
                SELECT 1 FROM account_move am
                 WHERE am.eh_borrowing_cost_id = c.id
            )
    """, (_REASON,))
