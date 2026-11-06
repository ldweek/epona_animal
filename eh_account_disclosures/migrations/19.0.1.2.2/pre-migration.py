# -*- coding: utf-8 -*-
"""Normalise duplicate disclosure dimensions before unique constraints."""


def migrate(cr, version):
    # Preserve the previously disclosed totals while collapsing ambiguous
    # duplicate stage rows.  The source stamp is cleared so the preparer must
    # repopulate before relying on an engine comparison again.
    cr.execute("""
        WITH grouped AS (
            SELECT note_id, stage,
                   (array_agg(id ORDER BY
                       CASE origin WHEN 'manual' THEN 0 ELSE 1 END, id))[1]
                       AS keep_id,
                   SUM(gross_carrying) AS gross_carrying,
                   SUM(allowance) AS allowance
              FROM eh_fin_credit_stage_line
             GROUP BY note_id, stage
            HAVING COUNT(*) > 1
        )
        UPDATE eh_fin_credit_stage_line line
           SET gross_carrying = grouped.gross_carrying,
               allowance = grouped.allowance,
               origin = 'manual',
               engine_linked = FALSE,
               engine_gross = 0,
               engine_allowance = 0
          FROM grouped
         WHERE line.id = grouped.keep_id
    """)
    cr.execute("""
        WITH ranked AS (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY note_id, stage
                ORDER BY CASE origin WHEN 'manual' THEN 0 ELSE 1 END, id
            ) AS row_no
              FROM eh_fin_credit_stage_line
        )
        DELETE FROM eh_fin_credit_stage_line line
         USING ranked
         WHERE line.id = ranked.id AND ranked.row_no > 1
    """)

    cr.execute("""
        WITH grouped AS (
            SELECT note_id, stage, MIN(id) AS keep_id,
                   SUM(opening) AS opening,
                   SUM(transfers_in) AS transfers_in,
                   SUM(transfers_out) AS transfers_out,
                   SUM(remeasurement) AS remeasurement,
                   SUM(writeoffs) AS writeoffs,
                   SUM(closing) AS closing
              FROM eh_fin_credit_recon_line
             GROUP BY note_id, stage
            HAVING COUNT(*) > 1
        )
        UPDATE eh_fin_credit_recon_line line
           SET opening = grouped.opening,
               transfers_in = grouped.transfers_in,
               transfers_out = grouped.transfers_out,
               remeasurement = grouped.remeasurement,
               writeoffs = grouped.writeoffs,
               closing = grouped.closing
          FROM grouped
         WHERE line.id = grouped.keep_id
    """)
    cr.execute("""
        WITH ranked AS (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY note_id, stage ORDER BY id
            ) AS row_no
              FROM eh_fin_credit_recon_line
        )
        DELETE FROM eh_fin_credit_recon_line line
         USING ranked
         WHERE line.id = ranked.id AND ranked.row_no > 1
    """)

    cr.execute("""
        WITH grouped AS (
            SELECT party_id, category,
                   (array_agg(id ORDER BY
                       CASE origin
                           WHEN 'manual' THEN 0
                           WHEN 'sbp' THEN 1
                           ELSE 2
                       END, id))[1] AS keep_id,
                   SUM(amount) AS amount,
                   string_agg(NULLIF(note, ''), '; ' ORDER BY id) AS note
              FROM eh_related_party_compensation
             GROUP BY party_id, category
            HAVING COUNT(*) > 1
        )
        UPDATE eh_related_party_compensation line
           SET amount = grouped.amount,
               origin = 'manual',
               note = COALESCE(grouped.note,
                   'Migrated aggregate of duplicate category rows')
          FROM grouped
         WHERE line.id = grouped.keep_id
    """)
    cr.execute("""
        WITH ranked AS (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY party_id, category
                ORDER BY CASE origin
                    WHEN 'manual' THEN 0 WHEN 'sbp' THEN 1 ELSE 2 END, id
            ) AS row_no
              FROM eh_related_party_compensation
        )
        DELETE FROM eh_related_party_compensation line
         USING ranked
         WHERE line.id = ranked.id AND ranked.row_no > 1
    """)
