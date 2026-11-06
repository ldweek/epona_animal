# -*- encoding: utf-8 -*-
"""Canonicalise historic cases before the commercial-partner constraint."""


def migrate(cr, version):
    del version
    cr.execute("""
        SELECT EXISTS (
            SELECT 1
              FROM eh_collections_case AS case_rec
              JOIN res_partner AS partner
                ON partner.id = case_rec.partner_id
             WHERE NOT case_rec.is_resolved
             GROUP BY case_rec.company_id,
                      COALESCE(partner.commercial_partner_id, partner.id)
            HAVING COUNT(*) > 1
        )
    """)
    has_open_duplicates = cr.fetchone()[0]
    cr.execute("""
        SELECT id
          FROM eh_collections_stage
         WHERE is_resolved
         ORDER BY sequence, id
         LIMIT 1
    """)
    row = cr.fetchone()
    if has_open_duplicates and not row:
        raise RuntimeError(
            "Cannot canonicalise collections cases: configure a resolved "
            "collections stage first."
        )
    resolved_stage_id = row[0] if row else None
    if has_open_duplicates:
        cr.execute("""
            WITH ranked AS (
                SELECT case_rec.id,
                       ROW_NUMBER() OVER (
                           PARTITION BY case_rec.company_id,
                                        COALESCE(
                                            partner.commercial_partner_id,
                                            partner.id
                                        )
                           ORDER BY case_rec.total_overdue_amount DESC,
                                    case_rec.id DESC
                       ) AS position
                  FROM eh_collections_case AS case_rec
                  JOIN res_partner AS partner
                    ON partner.id = case_rec.partner_id
                 WHERE NOT case_rec.is_resolved
            )
            UPDATE eh_collections_case AS case_rec
               SET stage_id = %s,
                   is_resolved = TRUE,
                   write_date = NOW()
              FROM ranked
             WHERE ranked.id = case_rec.id
               AND ranked.position > 1
        """, [resolved_stage_id])

    # Resolved history is retained; only its customer identity is rolled up.
    # For each duplicate open group, the strongest exposure remains open and
    # the other histories were closed above before this key update.
    cr.execute("""
        UPDATE eh_collections_case AS case_rec
           SET partner_id = COALESCE(
                   partner.commercial_partner_id,
                   partner.id
               ),
               write_date = NOW()
          FROM res_partner AS partner
         WHERE partner.id = case_rec.partner_id
           AND case_rec.partner_id IS DISTINCT FROM COALESCE(
                   partner.commercial_partner_id,
                   partner.id
               )
    """)
