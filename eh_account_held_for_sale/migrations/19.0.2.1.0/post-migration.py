# -*- coding: utf-8 -*-
"""Quarantine legacy linked classifications without IFRS 5.38 evidence."""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # An old linked classification may have paused the asset and posted an
    # HFS impairment, but it did not transfer gross PPE and its contras into a
    # separately presented HFS balance.  Neither the original accounting date
    # nor reviewer intent can be reconstructed safely during an upgrade. Keep
    # every historical byte, flag the gap, and require the manager's explicit
    # current-period correction workflow for active records.
    cr.execute(
        """
        UPDATE eh_held_for_sale
           SET asset_reclassification_quarantined = TRUE,
               asset_reclassification_quarantine_note = CASE
                   WHEN state = 'held' THEN
                       '19.0.2.1.0 upgrade audit: this active linked asset '
                       'predates sealed IFRS 5.38 gross/contra '
                       'reclassification evidence. Historical entries were '
                       'not fabricated. A manager must enter a current-period '
                       'correction date and workpaper basis, then run '
                       'Remediate IFRS 5.38 before any further event.'
                   ELSE
                       '19.0.2.1.0 upgrade audit: this completed linked '
                       'classification predates sealed IFRS 5.38 '
                       'gross/contra reclassification evidence. Historical '
                       'entries were preserved and no correction was '
                       'fabricated by the upgrade.'
               END,
               write_date = NOW()
         WHERE asset_id IS NOT NULL
           AND state IN ('held', 'sold', 'cancelled')
           AND asset_reclassification_move_id IS NULL
        """,
    )
    _logger.warning(
        "HFS 19.0.2.1.0 quarantined %d legacy linked classifications "
        "without IFRS 5.38 presentation evidence; no GL history was changed.",
        cr.rowcount,
    )

    cr.execute(
        """
        UPDATE eh_disposal_group AS group_record
           SET asset_reclassification_quarantined = TRUE,
               asset_reclassification_quarantine_note = CASE
                   WHEN group_record.state = 'held' THEN
                       '19.0.2.1.0 upgrade audit: this active linked disposal '
                       'group predates sealed IFRS 5.38 gross/contra '
                       'reclassification evidence. Historical entries were '
                       'not fabricated. A manager must enter a current-period '
                       'correction date and workpaper basis, then run '
                       'Remediate IFRS 5.38 before any further event.'
                   ELSE
                       '19.0.2.1.0 upgrade audit: this completed linked '
                       'disposal group predates sealed IFRS 5.38 gross/contra '
                       'reclassification evidence. Historical entries were '
                       'preserved and no correction was fabricated by the '
                       'upgrade.'
               END,
               write_date = NOW()
         WHERE group_record.state IN ('held', 'sold', 'cancelled')
           AND group_record.asset_reclassification_move_id IS NULL
           AND EXISTS (
               SELECT 1
                 FROM eh_disposal_group_line AS member
                WHERE member.group_id = group_record.id
                  AND member.asset_id IS NOT NULL
           )
        """,
    )
    _logger.warning(
        "HFS 19.0.2.1.0 quarantined %d legacy linked disposal groups "
        "without IFRS 5.38 presentation evidence; no GL history was changed.",
        cr.rowcount,
    )
