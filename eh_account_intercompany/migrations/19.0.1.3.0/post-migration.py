# -*- coding: utf-8 -*-
"""Quarantine unsafe legacy elimination destinations and stock fractions."""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # Never move or reverse historical journals automatically. Flag every
    # batch whose destination is a member/statutory ledger or has not been
    # explicitly established as a non-trading consolidation workspace.
    cr.execute(
        """
        UPDATE eh_ic_elimination_batch AS batch
           SET statutory_ledger_quarantined = TRUE,
               statutory_ledger_quarantine_note =
                   '19.0.1.3.0 upgrade audit: the legacy elimination '
                   'destination is a member legal entity or is not proven '
                   'to be a consolidation-only workspace. Historical '
                   'journals were preserved. Reset/reverse the batch, route '
                   'it to a distinct company marked Consolidation-only, and '
                   'recompute before reuse.',
               write_date = NOW()
          FROM res_company AS company
         WHERE company.id = batch.elimination_company_id
           AND (
                batch.elimination_company_id IN (
                    batch.company_a_id, batch.company_b_id
                )
                OR COALESCE(company.eh_ic_consolidation_only, FALSE) = FALSE
           )
        """,
    )
    statutory_count = cr.rowcount

    # Old stock-quant fractions have no exact invoice-line provenance and are
    # current-state rather than cutoff evidence. New columns intentionally
    # remain blank/unreviewed; posting and the consolidation hook fail closed
    # until a manager enters dated source-line workpaper evidence after a
    # recompute. Do not guess source lines from product/date proximity.
    cr.execute(
        """
        UPDATE eh_ic_unrealised_line
           SET source_move_line_id = NULL,
               inventory_evidence_date = NULL,
               inventory_evidence_reference = NULL,
               inventory_evidence_reviewed = FALSE
        """,
    )
    inventory_count = cr.rowcount
    _logger.warning(
        "Intercompany 19.0.1.3.0 quarantined %d unsafe elimination "
        "destinations and invalidated %d legacy aggregate stock fractions; "
        "no historical journal or inventory result was rewritten.",
        statutory_count, inventory_count,
    )
