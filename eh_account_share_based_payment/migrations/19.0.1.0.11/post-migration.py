# -*- encoding: utf-8 -*-
"""Backfill only terminal effective dates proved by linked SBP entries."""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        """
        WITH evidence AS (
            SELECT eh_sbp_plan_id AS plan_id, MAX(date) AS effective_date
              FROM account_move
             WHERE eh_sbp_plan_id IS NOT NULL
               AND state = 'posted'
             GROUP BY eh_sbp_plan_id
        )
        UPDATE eh_sbp_plan AS plan
           SET cancellation_date = evidence.effective_date
          FROM evidence
         WHERE plan.id = evidence.plan_id
           AND plan.state = 'cancelled'
           AND plan.cancellation_date IS NULL
        """
    )
    cancelled = cr.rowcount
    cr.execute(
        """
        WITH evidence AS (
            SELECT eh_sbp_plan_id AS plan_id, MAX(date) AS effective_date
              FROM account_move
             WHERE eh_sbp_plan_id IS NOT NULL
               AND state = 'posted'
             GROUP BY eh_sbp_plan_id
        )
        UPDATE eh_sbp_plan AS plan
           SET settlement_date = evidence.effective_date
          FROM evidence
         WHERE plan.id = evidence.plan_id
           AND plan.state = 'settled'
           AND plan.settlement_date IS NULL
        """
    )
    settled = cr.rowcount
    if cancelled or settled:
        _logger.info(
            "SBP 19.0.1.0.11 backfilled %d cancellation and %d settlement "
            "effective date(s) from linked posted-entry evidence.",
            cancelled,
            settled,
        )
