# -*- encoding: utf-8 -*-
"""Quarantine every legacy SBP output/ledger graph.

Legacy run outputs, source links, seals, and the cumulative plan anchor were
writable. Matching arithmetic, a matching journal shape, or a stamp written by
an intermediate build cannot prove the opening anchor used by that build.
Every history already present at upgrade is therefore quarantined. The new
immutable stamp protects evidence created only after this migration completes.
"""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        "ALTER TABLE eh_sbp_plan ADD COLUMN IF NOT EXISTS "
        "legacy_provenance_quarantined boolean DEFAULT false"
    )
    cr.execute(
        "ALTER TABLE eh_sbp_plan ADD COLUMN IF NOT EXISTS "
        "legacy_provenance_quarantine_note text"
    )
    cr.execute(
        "ALTER TABLE account_move ADD COLUMN IF NOT EXISTS "
        "eh_sbp_provenance_version integer DEFAULT 0"
    )
    cr.execute(
        """
        UPDATE eh_sbp_plan AS plan
           SET legacy_provenance_quarantined = TRUE,
               legacy_provenance_quarantine_note =
                   'Fail-closed SBP provenance audit: a posted period run, '
                   'plan-linked journal entry, or nonzero cumulative anchor '
                   'already existed at upgrade. Historical rows were '
                   'preserved and all SBP accounting transitions are blocked '
                   'pending review. No legacy/intermediate graph, stamp, or '
                   'matching shape was trusted.'
         WHERE EXISTS (
                   SELECT 1
                     FROM eh_sbp_period_run AS run
                    WHERE run.plan_id = plan.id
                      AND (run.state = 'posted' OR run.move_id IS NOT NULL)
               )
            OR EXISTS (
                   SELECT 1
                     FROM account_move AS move
                    WHERE move.eh_sbp_plan_id = plan.id
               )
            OR (
                   abs(COALESCE(plan.recognised_cumulative, 0)) > COALESCE((
                       SELECT currency.rounding / 2.0
                         FROM res_company AS company
                         JOIN res_currency AS currency
                           ON currency.id = company.currency_id
                        WHERE company.id = plan.company_id
                   ), 0.005)
               )
        """
    )
    if cr.rowcount:
        _logger.warning(
            "SBP provenance audit quarantined %d plan(s) carrying "
            "legacy posted-run, linked-move, or nonzero-anchor evidence. "
            "No intermediate graph or stamp was trusted.",
            cr.rowcount,
        )
