# -*- encoding: utf-8 -*-
"""Backfill company carrying bases and journal roles for Fair Value 1.1.7."""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # The old related currency field meant every historical item was in its
    # company's currency. Preserve that exact book carrying amount when the
    # field becomes a selectable measurement currency.
    cr.execute(
        """
        UPDATE eh_fair_value_item item
           SET prior_company_carrying = item.prior_carrying
          FROM res_company company
         WHERE company.id = item.company_id
           AND item.currency_id = company.currency_id
           AND COALESCE(item.prior_company_carrying, 0) = 0
           AND COALESCE(item.prior_carrying, 0) <> 0
        """,
    )
    carrying_rows = cr.rowcount

    # Existing English journal labels and, more importantly, their account
    # shape distinguish disposal/reserve entries. Everything else generated
    # by the legacy module is a remeasurement. New entries receive an exact
    # server-owned role at creation and do not rely on this compatibility
    # classification.
    cr.execute(
        """
        UPDATE account_move move
           SET eh_fair_value_move_role = CASE
               WHEN NOT EXISTS (
                   SELECT 1
                     FROM account_move_line line
                    WHERE line.move_id = move.id
                      AND line.account_id = item.balance_sheet_account_id
               )
               AND EXISTS (
                   SELECT 1
                     FROM account_move_line line
                    WHERE line.move_id = move.id
                      AND line.account_id = item.oci_account_id
               ) THEN 'reserve_settlement'
               WHEN EXISTS (
                   SELECT 1
                     FROM account_move_line line
                    WHERE line.move_id = move.id
                      AND (
                          line.account_id = item.settlement_account_id
                          OR line.name ILIKE 'Derecognise carrying %%'
                          OR line.name ILIKE 'Gain on derecognition %%'
                          OR line.name ILIKE 'Loss on derecognition %%'
                      )
               ) THEN 'derecognition'
               ELSE 'remeasurement'
           END
          FROM eh_fair_value_item item
         WHERE item.id = move.eh_fair_value_item_id
           AND move.eh_fair_value_move_role IS NULL
           AND move.reversed_entry_id IS NULL
        """,
    )
    role_rows = cr.rowcount

    # A legacy roll-forward may already link entries that the old broad date
    # pull misclassified. Remove only those links; the sealed journal and item
    # provenance remain untouched.
    cr.execute(
        """
        UPDATE account_move
           SET eh_fair_value_rollforward_id = NULL
         WHERE eh_fair_value_rollforward_id IS NOT NULL
           AND eh_fair_value_move_role <> 'remeasurement'
        """,
    )
    unlinked_rows = cr.rowcount

    _logger.info(
        "Fair Value 19.0.1.1.7 backfilled %d company carrying bases, "
        "classified %d legacy journal roles, and removed %d non-measurement "
        "roll-forward links.",
        carrying_rows,
        role_rows,
        unlinked_rows,
    )
