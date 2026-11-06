# -*- coding: utf-8 -*-
"""Quarantine legacy account-wide scope and unmeasured declassifications."""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # Reconstruct the pre-classification carrying amount only where the exact
    # item-owned posted impairment legs make it algebraically provable.
    cr.execute(
        """
        UPDATE eh_held_for_sale AS item
           SET preclassification_carrying_amount =
               item.carrying_amount + COALESCE((
                   SELECT sum(line.debit - line.credit)
                     FROM account_move AS move
                     JOIN account_move_line AS line
                       ON line.move_id = move.id
                    WHERE move.eh_held_for_sale_id = item.id
                      AND move.state = 'posted'
                      AND line.account_id = item.impairment_account_id
               ), 0),
               write_date = NOW()
         WHERE item.state = 'held'
           AND item.preclassification_carrying_amount IS NULL
        """,
    )
    cr.execute(
        """
        UPDATE eh_disposal_group_line AS line
           SET preclassification_carrying_amount =
               line.carrying_amount + COALESCE(line.cumulative_writedown, 0),
               write_date = NOW()
          FROM eh_disposal_group AS grp
         WHERE grp.id = line.group_id
           AND grp.state = 'held'
           AND line.preclassification_carrying_amount IS NULL
        """,
    )

    # Legacy Cancel was a bare state flip/resume and therefore has neither the
    # IFRS 5.27 lower-of measurement nor a linked JE. Retain the records and
    # make the missing evidence explicit; never manufacture a target/move.
    cr.execute(
        """
        UPDATE eh_held_for_sale AS item
           SET declassification_evidence_quarantined = TRUE,
               declassification_evidence_quarantine_note =
                   '19.0.2.0.5 upgrade audit: this legacy cancellation '
                   'predates the mandatory IFRS 5.27 adjusted-carrying and '
                   'recoverable-amount measurement. No target or journal '
                   'entry was fabricated.',
               write_date = NOW()
         WHERE item.state = 'cancelled'
           AND item.declassified_at IS NULL
        """,
    )
    item_declass = cr.rowcount
    cr.execute(
        """
        UPDATE eh_disposal_group AS grp
           SET declassification_evidence_quarantined = TRUE,
               declassification_evidence_quarantine_note =
                   '19.0.2.0.5 upgrade audit: this legacy cancellation '
                   'predates member-level IFRS 5.27 measurement. No member '
                   'targets or journal entries were fabricated.',
               write_date = NOW()
         WHERE grp.state = 'cancelled'
           AND grp.declassified_at IS NULL
        """,
    )
    group_declass = cr.rowcount

    # Account membership never proves which operation produced a journal
    # item. Existing account-wide tags are retained for discovery only, while
    # the report intentionally returns no amount until a manager selects exact
    # posted P&L journal items under the new workflow.
    cr.execute(
        """
        UPDATE eh_held_for_sale AS item
           SET discontinued_scope_quarantined = TRUE,
               discontinued_scope_quarantine_note =
                   '19.0.2.0.5 upgrade audit: legacy discontinued scope '
                   'identified whole accounts and could include continuing '
                   'operations or pre-classification activity. Select exact '
                   'posted P&L journal items and run Tag Discontinued P&L.',
               write_date = NOW()
         WHERE item.discontinued_tag_id IS NOT NULL
           AND NOT EXISTS (
                SELECT 1 FROM eh_hfs_disc_evidence_move_line_rel AS rel
                 WHERE rel.item_id = item.id
           )
        """,
    )
    item_scope = cr.rowcount
    cr.execute(
        """
        UPDATE eh_disposal_group AS grp
           SET discontinued_scope_quarantined = TRUE,
               discontinued_scope_quarantine_note =
                   '19.0.2.0.5 upgrade audit: legacy discontinued scope '
                   'identified whole accounts and could include continuing '
                   'operations or pre-classification activity. Select exact '
                   'posted P&L journal items and run Tag Discontinued P&L.',
               write_date = NOW()
         WHERE grp.discontinued_tag_id IS NOT NULL
           AND NOT EXISTS (
                SELECT 1 FROM eh_dg_disc_evidence_move_line_rel AS rel
                 WHERE rel.group_id = grp.id
           )
        """,
    )
    group_scope = cr.rowcount
    _logger.warning(
        "HFS 19.0.2.0.5 quarantined %d item/%d group legacy "
        "declassifications and %d item/%d group account-wide discontinued "
        "scopes; no historical figures or links were invented.",
        item_declass, group_declass, item_scope, group_scope,
    )
