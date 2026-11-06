# -*- encoding: utf-8 -*-
"""Quarantine stale approvals and normalize deterministic ownership."""

import logging

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})

    cr.execute(
        """
        UPDATE eh_ap_intake_line AS line
           SET company_id = intake.company_id,
               write_uid = %s,
               write_date = NOW()
          FROM eh_ap_intake AS intake
         WHERE intake.id = line.intake_id
           AND line.company_id IS DISTINCT FROM intake.company_id
        """,
        [SUPERUSER_ID],
    )
    normalized_lines = cr.rowcount

    cr.execute(
        """
        UPDATE eh_ap_intake_line_decision AS decision
           SET intake_id = line.intake_id,
               company_id = intake.company_id,
               write_uid = %s,
               write_date = NOW()
          FROM eh_ap_intake_line AS line
          JOIN eh_ap_intake AS intake ON intake.id = line.intake_id
         WHERE decision.line_id = line.id
           AND (decision.intake_id IS DISTINCT FROM line.intake_id
                OR decision.company_id IS DISTINCT FROM intake.company_id)
        """,
        [SUPERUSER_ID],
    )
    normalized_decisions = cr.rowcount

    cr.execute(
        """
        UPDATE eh_ap_duplicate_override_log AS log
           SET company_id = intake.company_id,
               write_uid = %s,
               write_date = NOW()
          FROM eh_ap_intake AS intake
         WHERE log.intake_id = intake.id
           AND log.company_id IS DISTINCT FROM intake.company_id
        """,
        [SUPERUSER_ID],
    )
    normalized_duplicates = cr.rowcount

    cr.execute(
        """
        UPDATE eh_ap_payment_hold_log AS log
           SET company_id = move.company_id,
               write_uid = %s,
               write_date = NOW()
          FROM account_move AS move
         WHERE log.move_id = move.id
           AND log.company_id IS DISTINCT FROM move.company_id
        """,
        [SUPERUSER_ID],
    )
    normalized_holds = cr.rowcount

    # v1 fingerprints did not bind sibling consumption, PO discounts, or the
    # effective FX rate.  Preserve all append-only decision rows, but never
    # manufacture a v2 approval: live work returns to parsed for re-review.
    cr.execute(
        """
        UPDATE eh_ap_intake_line AS line
           SET purchase_order_line_id = NULL,
               po_qty = 0,
               po_price = 0,
               received_qty = 0,
               match_status = 'pending',
               qty_diff = 0,
               price_diff = 0,
               qty_diff_pct = 0,
               price_diff_pct = 0,
               match_notes = NULL,
               override_reason = NULL,
               overridden_at = NULL,
               overridden_by_id = NULL,
               decision_log_id = NULL,
               match_evidence_fingerprint = NULL,
               write_uid = %s,
               write_date = NOW()
          FROM eh_ap_intake AS intake
         WHERE intake.id = line.intake_id
           AND intake.state IN ('matched', 'exception')
        """,
        [SUPERUSER_ID],
    )
    reset_lines = cr.rowcount

    cr.execute(
        """
        UPDATE eh_ap_intake
           SET state = 'parsed',
               matched_at = NULL,
               matched_by_id = NULL,
               duplicate_intake_id = NULL,
               duplicate_move_id = NULL,
               duplicate_override_log_id = NULL,
               duplicate_override_reason = NULL,
               duplicate_overridden_at = NULL,
               duplicate_overridden_by_id = NULL,
               duplicate_override_fingerprint = NULL,
               duplicate_override_request_reason = NULL,
               write_uid = %s,
               write_date = NOW()
         WHERE state IN ('matched', 'exception')
        """,
        [SUPERUSER_ID],
    )
    quarantined_intakes = cr.rowcount

    Move = env['account.move'].sudo()
    last_id = 0
    recomputed_moves = 0
    while True:
        moves = Move.search([
            ('id', '>', last_id),
            ('move_type', '=', 'in_invoice'),
        ], order='id', limit=1000)
        if not moves:
            break
        computed = moves.with_context(
            eh_ap_payment_control_internal=True,
        )
        computed._compute_eh_release_to_pay()
        computed.flush_recordset(['eh_release_to_pay'])
        recomputed_moves += len(moves)
        last_id = moves[-1].id

    _logger.warning(
        "AP automation 19.0.1.4.2: normalized ownership on %d line(s), %d "
        "decision(s), %d duplicate log(s), and %d hold log(s); quarantined "
        "%d live intake(s) and reset %d line(s) for v2 review; recomputed "
        "release status on %d vendor bill(s). No approval evidence was "
        "fabricated.",
        normalized_lines, normalized_decisions, normalized_duplicates,
        normalized_holds, quarantined_intakes, reset_lines,
        recomputed_moves,
    )
