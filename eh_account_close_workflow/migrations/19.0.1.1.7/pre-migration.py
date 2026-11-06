# -*- encoding: utf-8 -*-
"""Bound legacy periods and enforce durable checklist/task invariants."""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # A one-sided legacy period was already treated as unbounded by the old
    # overlap check. Deriving the missing edge within its known month narrows
    # rather than broadens that ownership interval.
    cr.execute(
        """
        UPDATE eh_close_run
           SET period_from = date_trunc('month', period_to)::date,
               write_uid = 1,
               write_date = NOW()
         WHERE period_from IS NULL
           AND period_to IS NOT NULL
        """
    )
    partial_from = cr.rowcount
    cr.execute(
        """
        UPDATE eh_close_run
           SET period_to = (
                   date_trunc('month', period_from)
                   + interval '1 month - 1 day'
               )::date,
               write_uid = 1,
               write_date = NOW()
         WHERE period_to IS NULL
           AND period_from IS NOT NULL
        """
    )
    partial_to = cr.rowcount

    # Fully undated scratch runs did not participate in the old overlap
    # control. Place them in distinct months immediately before the company's
    # earliest bounded run (or before this month when none exists), preserving
    # every record while preventing new whole-ledger scans or mutual overlap.
    cr.execute(
        """
        WITH company_floor AS (
            SELECT company_id,
                   COALESCE(
                       date_trunc('month', MIN(period_from)),
                       date_trunc('month', CURRENT_DATE)
                   )::date AS floor_month
              FROM eh_close_run
             WHERE period_from IS NOT NULL
             GROUP BY company_id
        ), ranked AS (
            SELECT run.id,
                   COALESCE(
                       floor.floor_month,
                       date_trunc('month', CURRENT_DATE)::date
                   ) AS floor_month,
                   ROW_NUMBER() OVER (
                       PARTITION BY run.company_id
                       ORDER BY run.create_date NULLS LAST, run.id
                   ) AS month_offset
              FROM eh_close_run AS run
              LEFT JOIN company_floor AS floor
                ON floor.company_id = run.company_id
             WHERE run.period_from IS NULL
               AND run.period_to IS NULL
        )
        UPDATE eh_close_run AS run
           SET period_from = (
                   ranked.floor_month
                   - ranked.month_offset * interval '1 month'
               )::date,
               period_to = (
                   ranked.floor_month
                   - ranked.month_offset * interval '1 month'
                   + interval '1 month - 1 day'
               )::date,
               write_uid = 1,
               write_date = NOW()
          FROM ranked
         WHERE run.id = ranked.id
        """
    )
    undated = cr.rowcount

    cr.execute(
        """
        UPDATE eh_close_task
           SET blocked_reason =
                   'Legacy blocked task migrated without a recorded reason.',
               write_uid = 1,
               write_date = NOW()
         WHERE state = 'blocked'
           AND LENGTH(BTRIM(COALESCE(blocked_reason, ''))) < 10
        """
    )
    blocked = cr.rowcount

    # PostgreSQL UNIQUE permits repeated NULL company values. Keep the oldest
    # shared code and deterministically rename later legacy duplicates before
    # installing the partial unique index used by current code.
    cr.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (PARTITION BY code ORDER BY id) AS rank
              FROM eh_close_checklist
             WHERE company_id IS NULL
        )
        UPDATE eh_close_checklist AS checklist
           SET code = checklist.code || '_legacy_' || checklist.id,
               write_uid = 1,
               write_date = NOW()
          FROM ranked
         WHERE checklist.id = ranked.id
           AND ranked.rank > 1
        """
    )
    renamed = cr.rowcount
    cr.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
            eh_close_checklist_shared_code_uniq
        ON eh_close_checklist (code)
        WHERE company_id IS NULL
        """
    )

    _logger.warning(
        "Close Workflow 19.0.1.1.7 bounded %d one-sided start, %d "
        "one-sided end and %d undated run(s); annotated %d legacy blocked "
        "task(s); renamed %d duplicate shared checklist code(s).",
        partial_from,
        partial_to,
        undated,
        blocked,
        renamed,
    )
