# -*- encoding: utf-8 -*-
"""Quarantine all legacy SO/PO mirror links without durable provenance.

Every pre-control link field was ordinary RPC-writable. Reciprocal links,
matching companies, and matching partners are therefore self-supplied shape,
not proof. Preserve every order and FK, mark every linked header unverified,
and let runtime refuse further confirmation pending controlled review.
"""


_REASON = (
    'Legacy inter-company mirror links predate durable engine provenance; '
    'all orders and links were preserved for controlled review.'
)


def migrate(cr, version):
    if not version:
        return
    for table in ('sale_order', 'purchase_order'):
        cr.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
            "eh_ic_provenance_verified boolean DEFAULT FALSE"
        )
        cr.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
            "eh_ic_integrity_review_reason text"
        )
    cr.execute("""
        UPDATE sale_order
           SET eh_ic_provenance_verified = FALSE,
               eh_ic_integrity_review_reason = %s
         WHERE eh_ic_mirror_po_id IS NOT NULL
            OR eh_ic_origin_po_id IS NOT NULL
    """, (_REASON,))
    cr.execute("""
        UPDATE purchase_order
           SET eh_ic_provenance_verified = FALSE,
               eh_ic_integrity_review_reason = %s
         WHERE eh_ic_mirror_so_id IS NOT NULL
            OR eh_ic_origin_so_id IS NOT NULL
    """, (_REASON,))
