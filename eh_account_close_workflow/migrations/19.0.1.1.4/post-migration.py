# -*- encoding: utf-8 -*-
"""Backfill append-only close sign-offs; quarantine ambiguous reopen cycles."""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    for event_type, user_column, time_column in (
        ('prepare', 'prepared_by_id', 'prepared_at'),
        ('review', 'reviewed_by_id', 'reviewed_at'),
        ('approve', 'approved_by_id', 'approved_at'),
        ('reopen', 'reopened_by_id', 'reopened_at'),
    ):
        cr.execute(
            f"""
            INSERT INTO eh_close_signoff
                (run_id, event_type, user_id, event_at,
                 create_uid, create_date, write_uid, write_date)
            SELECT run.id, %s, run.{user_column}, run.{time_column},
                   1, NOW(), 1, NOW()
              FROM eh_close_run AS run
             WHERE run.{user_column} IS NOT NULL
               AND run.{time_column} IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1
                     FROM eh_close_signoff AS event
                    WHERE event.run_id = run.id
                      AND event.event_type = %s
                      AND event.user_id = run.{user_column}
                      AND event.event_at = run.{time_column}
               )
            """,
            [event_type, event_type],
        )
        _logger.info(
            "eh_account_close_workflow 19.0.1.1.4: backfilled %d %s "
            "sign-off events.",
            cr.rowcount,
            event_type,
        )

    # Old reopened runs retained only first-cycle preparer timestamps, while
    # later review/approval fields could be overwritten. Any reopened cycle
    # still in flight therefore lacks provable current-cycle SoD identities.
    # Return it to reopened so a new Start action captures fresh evidence.
    cr.execute(
        """
        UPDATE eh_close_run
           SET state = 'reopened',
               write_uid = 1,
               write_date = NOW()
         WHERE reopened_by_id IS NOT NULL
           AND state IN ('in_progress', 'pending_approval')
        """,
    )
    if cr.rowcount:
        _logger.warning(
            "eh_account_close_workflow 19.0.1.1.4: returned %d legacy "
            "reopened runs to reopened because current-cycle preparer and "
            "reviewer evidence was not reconstructible.",
            cr.rowcount,
        )
