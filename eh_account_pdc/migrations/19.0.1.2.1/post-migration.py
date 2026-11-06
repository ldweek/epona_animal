# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Backfill immutable counterparty names on pre-1.2.1 cheque evidence."""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # Existing cheque-to-move links are authoritative source provenance, so
    # they can be backfilled exactly (unlike approval-time values). Preserve
    # any newer marker already present to keep retries idempotent.
    link_cte = """
        WITH links AS (
            SELECT id AS cheque_id, present_move_id AS move_id
              FROM eh_cheque WHERE present_move_id IS NOT NULL
            UNION ALL
            SELECT id, clear_move_id FROM eh_cheque
             WHERE clear_move_id IS NOT NULL
            UNION ALL
            SELECT id, bounce_move_id FROM eh_cheque
             WHERE bounce_move_id IS NOT NULL
            UNION ALL
            SELECT id, bounce_charge_move_id FROM eh_cheque
             WHERE bounce_charge_move_id IS NOT NULL
        ), unique_links AS (
            SELECT move_id, MIN(cheque_id) AS cheque_id
              FROM links
             GROUP BY move_id
            HAVING COUNT(*) = 1
        )
    """
    cr.execute(
        link_cte
        + "SELECT COUNT(*) FROM ("
          "SELECT move_id FROM links GROUP BY move_id HAVING COUNT(*) > 1"
          ") conflicts",
    )
    conflicts = cr.fetchone()[0]
    if conflicts:
        _logger.warning(
            "eh_account_pdc 19.0.1.2.1: left %d ambiguously linked legacy "
            "moves without provenance; their cheque workflows remain "
            "fail-closed pending controlled review.",
            conflicts,
        )

    for column, transition in (
        ('present_move_id', 'present'),
        ('clear_move_id', 'clear'),
        ('bounce_move_id', 'bounce'),
        ('bounce_charge_move_id', 'bounce_charge'),
    ):
        cr.execute(
            link_cte + f"""
            UPDATE account_move AS move
               SET eh_pdc_cheque_id = cheque.id,
                   eh_pdc_transition = %s
              FROM eh_cheque AS cheque, unique_links
             WHERE move.id = cheque.{column}
               AND cheque.{column} IS NOT NULL
               AND unique_links.move_id = move.id
               AND unique_links.cheque_id = cheque.id
               AND move.eh_pdc_cheque_id IS NULL
            """,
            [transition],
        )
        _logger.info(
            "eh_account_pdc 19.0.1.2.1: backfilled %d %s move markers.",
            cr.rowcount,
            transition,
        )

    # Drafts deliberately remain live: their counterparty may still be
    # corrected and action_register will take the authoritative snapshot.
    # Every non-draft legacy row is retained evidence, including a cheque
    # cancelled before presentation, so seal the best context-independent
    # name available at upgrade time.  Never overwrite an existing snapshot;
    # this keeps the migration idempotent and preserves imports already fixed
    # by an operator before a migration retry.
    cr.execute(
        """
        UPDATE eh_cheque AS cheque
           SET payee_name_snapshot = COALESCE(
                   NULLIF(BTRIM(partner.complete_name), ''),
                   NULLIF(BTRIM(partner.name), '')
               )
          FROM res_partner AS partner
         WHERE partner.id = cheque.partner_id
           AND cheque.state != 'draft'
           AND COALESCE(BTRIM(cheque.payee_name_snapshot), '') = ''
           AND COALESCE(
                   NULLIF(BTRIM(partner.complete_name), ''),
                   NULLIF(BTRIM(partner.name), '')
               ) IS NOT NULL
        """,
    )
    _logger.info(
        "eh_account_pdc 19.0.1.2.1: backfilled %d immutable payee name "
        "snapshots on legacy cheque evidence.",
        cr.rowcount,
    )
