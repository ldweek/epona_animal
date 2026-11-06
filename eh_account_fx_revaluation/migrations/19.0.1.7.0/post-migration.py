# -*- encoding: utf-8 -*-
"""Backfill explicit dates and independently evidenced hedge measurements."""

from odoo.addons.eh_account_fx_revaluation.migration_utils import (
    migrate_fx_provenance,
)


def migrate(cr, version):
    if not version:
        return

    # Old movement payloads used effective_portion as the hedged-item mirror.
    # Preserve their exact posted journal substance while making that inferred
    # legacy measurement explicit. New movements must supply it independently.
    cr.execute("""
        UPDATE eh_fx_hedge_movement
           SET hedged_item_change = -COALESCE(effective_portion, 0.0),
               ineffective_portion = COALESCE(total_change, 0.0)
                   - COALESCE(effective_portion, 0.0),
               reclassification_date = COALESCE(
                   (SELECT move.date
                      FROM account_move AS move
                     WHERE move.id =
                           eh_fx_hedge_movement.reclassification_move_id),
                   eh_fx_hedge_movement.movement_date
               )
    """)

    cr.execute("""
        UPDATE eh_fx_revaluation_run AS run
           SET reversal_date = COALESCE(
               reversal.date,
               run.revaluation_date + 1
           )
          FROM account_move AS reversal
         WHERE reversal.id = run.reversal_move_id
    """)
    cr.execute("""
        UPDATE eh_fx_revaluation_run
           SET reversal_date = revaluation_date + 1
         WHERE reversal_date IS NULL
    """)

    # Pre-1.7 partial CTA behavior represented associates/JVs. Preserve that
    # meaning; new subsidiary partial disposals require explicit NCI treatment.
    cr.execute("""
        UPDATE eh_fx_cta_position
           SET operation_type = CASE
                   WHEN disposal_pct < 100.0
                   THEN 'associate_joint_venture'
                   ELSE 'subsidiary'
               END,
               disposal_date = COALESCE(
                   (SELECT move.date
                      FROM account_move AS move
                     WHERE move.id = eh_fx_cta_position.disposal_move_id),
                   CURRENT_DATE
               )
         WHERE disposal_move_id IS NOT NULL
    """)
    cr.execute("""
        UPDATE eh_fx_cta_position
           SET operation_type = CASE
                   WHEN disposal_pct < 100.0
                   THEN 'associate_joint_venture'
                   ELSE 'subsidiary'
               END,
               disposal_date = COALESCE(disposal_date, CURRENT_DATE)
         WHERE disposal_move_id IS NULL
    """)

    migrate_fx_provenance(cr)
