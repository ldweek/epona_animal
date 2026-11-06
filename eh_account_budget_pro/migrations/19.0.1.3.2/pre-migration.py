# -*- encoding: utf-8 -*-
"""Quarantine ambiguous legacy commitment source duplicates.

Rows are never deleted or merged: doing so would invent which encumbrance was
economically authoritative. Every row in an ambiguous source-key group is
retained, released from live availability, and marked inactive for review.
"""


def _table_exists(cr, table_name):
    cr.execute("SELECT to_regclass(%s)", (table_name,))
    return bool(cr.fetchone()[0])


def migrate(cr, version):
    if not version:
        return
    if not _table_exists(cr, 'eh_budget_commitment'):
        return
    cr.execute(
        "ALTER TABLE eh_budget_commitment "
        "ADD COLUMN IF NOT EXISTS active boolean DEFAULT TRUE"
    )
    cr.execute(
        "ALTER TABLE eh_budget_commitment "
        "ADD COLUMN IF NOT EXISTS eh_integrity_review_reason text"
    )
    cr.execute(
        "UPDATE eh_budget_commitment SET active = TRUE "
        "WHERE active IS NULL"
    )
    cr.execute(
        """
        WITH duplicate_keys AS (
            SELECT source_model, source_id, source_line_id, budget_line_id
              FROM eh_budget_commitment
             WHERE active
               AND source_model IS NOT NULL
               AND source_model NOT IN ('', 'manual')
             GROUP BY source_model, source_id, source_line_id, budget_line_id
            HAVING count(*) > 1
        )
        UPDATE eh_budget_commitment c
           SET active = FALSE,
               state = 'released',
               eh_integrity_review_reason =
                   'legacy-duplicate-source-key-preserved-for-review'
          FROM duplicate_keys d
         WHERE c.source_model = d.source_model
           AND c.source_id = d.source_id
           AND c.source_line_id = d.source_line_id
           AND c.budget_line_id = d.budget_line_id
        """
    )
