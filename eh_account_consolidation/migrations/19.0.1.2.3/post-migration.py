# -*- encoding: utf-8 -*-
"""Backfill append-only consolidation move provenance for legacy runs."""


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        INSERT INTO eh_consol_run_move_history_rel (run_id, move_id)
             SELECT run.id, run.move_id
               FROM eh_consol_run AS run
              WHERE run.move_id IS NOT NULL
        ON CONFLICT DO NOTHING
        """,
    )
