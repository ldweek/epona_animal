# -*- encoding: utf-8 -*-
"""Quarantine pre-1.0.4 SO/PO pairs with unsafe lifecycle shape.

Version 1.0.3 made provenance server-owned, but its cancel/reset/lock actions
still moved one header at a time.  A verified pair can therefore already have
one cancelled side and one live side, a broken reciprocal link, or divergent
locks after both sides were confirmed.  Preserve every legal document and FK,
remove the engine-verification seal on the complete referenced graph, and let
the 1.0.4 runtime block confirmation and downstream invoicing until review.
"""


_REASON = (
    'Pre-1.0.4 inter-company SO/PO lifecycle or reciprocal provenance is '
    'inconsistent; all orders and links were preserved for controlled review.'
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

    def _has_column(table, column):
        cr.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_attribute "
            "WHERE attrelid = %s::regclass AND attname = %s "
            "AND attnum > 0 AND NOT attisdropped)",
            (table, column),
        )
        return cr.fetchone()[0]

    sale_locked = (
        "COALESCE(s.locked, FALSE)"
        if _has_column('sale_order', 'locked') else "(s.state = 'done')"
    )
    purchase_locked = (
        "COALESCE(p.locked, FALSE)"
        if _has_column('purchase_order', 'locked') else "(p.state = 'done')"
    )

    cr.execute("""
        CREATE TEMP TABLE eh_ic_so_po_104_quarantine (
            sale_id integer,
            purchase_id integer,
            PRIMARY KEY (sale_id, purchase_id)
        ) ON COMMIT DROP
    """)
    cr.execute(f"""
        INSERT INTO eh_ic_so_po_104_quarantine (sale_id, purchase_id)
        WITH pairs AS (
            SELECT s.id AS sale_id, p.id AS purchase_id
              FROM sale_order s
              JOIN purchase_order p ON p.id = s.eh_ic_mirror_po_id
            UNION
            SELECT s.id, p.id
              FROM sale_order s
              JOIN purchase_order p ON p.id = s.eh_ic_origin_po_id
            UNION
            SELECT s.id, p.id
              FROM purchase_order p
              JOIN sale_order s ON s.id = p.eh_ic_mirror_so_id
            UNION
            SELECT s.id, p.id
              FROM purchase_order p
              JOIN sale_order s ON s.id = p.eh_ic_origin_so_id
        )
        SELECT s.id, p.id
          FROM pairs pair
          JOIN sale_order s ON s.id = pair.sale_id
          JOIN purchase_order p ON p.id = pair.purchase_id
         WHERE NOT (
                   COALESCE((
                       s.eh_ic_mirror_po_id = p.id
                       AND s.eh_ic_origin_po_id IS NULL
                       AND p.eh_ic_origin_so_id = s.id
                       AND p.eh_ic_mirror_so_id IS NULL
                   ), FALSE)
                   <>
                   COALESCE((
                       p.eh_ic_mirror_so_id = s.id
                       AND p.eh_ic_origin_so_id IS NULL
                       AND s.eh_ic_origin_po_id = p.id
                       AND s.eh_ic_mirror_po_id IS NULL
                   ), FALSE)
               )
            OR s.company_id = p.company_id
            OR NOT COALESCE(s.eh_ic_provenance_verified, FALSE)
            OR NOT COALESCE(p.eh_ic_provenance_verified, FALSE)
            OR s.eh_ic_integrity_review_reason IS NOT NULL
            OR p.eh_ic_integrity_review_reason IS NOT NULL
            OR ((s.state = 'cancel') <> (p.state = 'cancel'))
            OR s.state NOT IN ('draft', 'sent', 'sale', 'done', 'cancel')
            OR p.state NOT IN (
                'draft', 'sent', 'to approve', 'purchase', 'done', 'cancel'
            )
            OR (
                s.state IN ('sale', 'done')
                AND p.state IN ('purchase', 'done')
                AND {sale_locked} <> {purchase_locked}
            )
        ON CONFLICT DO NOTHING
    """)
    cr.execute("""
        UPDATE sale_order s
           SET eh_ic_provenance_verified = FALSE,
               eh_ic_integrity_review_reason = COALESCE(
                   s.eh_ic_integrity_review_reason, %s
               )
         WHERE s.id IN (
             SELECT sale_id FROM eh_ic_so_po_104_quarantine
         )
    """, (_REASON,))
    cr.execute("""
        UPDATE purchase_order p
           SET eh_ic_provenance_verified = FALSE,
               eh_ic_integrity_review_reason = COALESCE(
                   p.eh_ic_integrity_review_reason, %s
               )
         WHERE p.id IN (
             SELECT purchase_id FROM eh_ic_so_po_104_quarantine
         )
    """, (_REASON,))
