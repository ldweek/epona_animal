# -*- encoding: utf-8 -*-
"""Backfill append-only elimination move provenance for legacy batches."""


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        INSERT INTO eh_ic_elimination_batch_move_history_rel
                    (batch_id, move_id)
             SELECT batch.id, batch.move_id
               FROM eh_ic_elimination_batch AS batch
              WHERE batch.move_id IS NOT NULL
        ON CONFLICT DO NOTHING
        """,
    )
    cr.execute(
        """
        INSERT INTO eh_ic_elimination_batch_move_history_rel
                    (batch_id, move_id)
             SELECT batch.id, batch.reversal_move_id
               FROM eh_ic_elimination_batch AS batch
              WHERE batch.reversal_move_id IS NOT NULL
        ON CONFLICT DO NOTHING
        """,
    )
