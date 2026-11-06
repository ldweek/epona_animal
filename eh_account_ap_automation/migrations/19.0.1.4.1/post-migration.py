# -*- encoding: utf-8 -*-
"""Preserve legacy AP evidence and quarantine unprovable live approvals.

Pre-1.4.1 matches had no immutable source/PO fingerprint, line decisions were
mutable fields, and posted bills carried no unique intake provenance or suite
seal.  Never manufacture approval proof: retain posted accounting entries,
backfill only unambiguous source links, seal those entries, copy observable
legacy decisions into explicitly labelled append-only rows, and return live
matched/exception intakes to parsed for a new review.
"""

import hashlib
import json
import logging

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)


def _legacy_fingerprint(kind, values):
    payload = {
        'schema': 'eh-ap-legacy-observation-v1',
        'kind': kind,
        'values': values,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _legacy_reason(reason):
    reason = (reason or '').strip()
    if len(reason) <= 8000:
        return reason
    digest = hashlib.sha256(reason.encode('utf-8')).hexdigest()
    suffix = "\n[legacy reason truncated; full-text SHA-256: %s]" % digest
    return reason[:8000 - len(suffix)] + suffix


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})

    cr.execute(
        """
        SELECT COUNT(*)
          FROM (
                SELECT move_id
                  FROM eh_ap_intake
                 WHERE move_id IS NOT NULL
                 GROUP BY move_id
                HAVING COUNT(*) > 1
               ) AS conflicts
        """,
    )
    conflicting_moves = cr.fetchone()[0]

    cr.execute(
        """
        WITH unique_links AS (
            SELECT move_id, MIN(id) AS intake_id
              FROM eh_ap_intake
             WHERE move_id IS NOT NULL
             GROUP BY move_id
            HAVING COUNT(*) = 1
        )
        UPDATE account_move AS move
           SET eh_ap_intake_id = links.intake_id,
               eh_sealed = CASE
                   WHEN move.state = 'posted' THEN TRUE
                   ELSE move.eh_sealed
               END,
               write_uid = %s,
               write_date = NOW()
          FROM unique_links AS links
         WHERE move.id = links.move_id
           AND (move.eh_ap_intake_id IS NULL
                OR move.eh_ap_intake_id = links.intake_id)
        """,
        [SUPERUSER_ID],
    )
    linked_moves = cr.rowcount

    DuplicateLog = env['eh.ap.duplicate.override.log'].sudo()
    legacy_duplicates = env['eh.ap.intake'].sudo().search([
        ('duplicate_override_reason', '!=', False),
        ('duplicate_override_log_ids', '=', False),
    ], order='id')
    duplicate_logs = 0
    for intake in legacy_duplicates:
        full_reason = (intake.duplicate_override_reason or '').strip()
        reason = _legacy_reason(full_reason)
        if not reason:
            continue
        values = {
            'intake_id': intake.id,
            'duplicate_intake_id': intake.duplicate_intake_id.id or 0,
            'duplicate_move_id': intake.duplicate_move_id.id or 0,
            'reason': full_reason,
            'actor_id': (
                intake.duplicate_overridden_by_id.id
                or intake.write_uid.id
                or SUPERUSER_ID
            ),
            'event_at': str(
                intake.duplicate_overridden_at
                or intake.write_date
                or intake.create_date
                or ''
            ),
        }
        DuplicateLog.create({
            'intake_id': intake.id,
            'duplicate_intake_id': intake.duplicate_intake_id.id,
            'duplicate_move_id': intake.duplicate_move_id.id,
            'reason': reason,
            'evidence_fingerprint': _legacy_fingerprint(
                'duplicate-override', values,
            ),
            'overridden_by_id': values['actor_id'],
            'overridden_at': (
                intake.duplicate_overridden_at
                or intake.write_date
                or intake.create_date
            ),
        })
        duplicate_logs += 1

    DecisionLog = env['eh.ap.intake.line.decision'].sudo()
    legacy_lines = env['eh.ap.intake.line'].sudo().search([
        ('match_status', 'in', ['overridden', 'rejected']),
        ('override_reason', '!=', False),
        ('decision_log_ids', '=', False),
    ], order='id')
    decision_logs = 0
    for line in legacy_lines:
        full_reason = (line.override_reason or '').strip()
        reason = _legacy_reason(full_reason)
        if not reason:
            continue
        decision = 'override' if line.match_status == 'overridden' else 'reject'
        values = {
            'line_id': line.id,
            'intake_id': line.intake_id.id,
            'decision': decision,
            'reason': full_reason,
            'actor_id': (
                line.overridden_by_id.id or line.write_uid.id or SUPERUSER_ID
            ),
            'event_at': str(
                line.overridden_at or line.write_date or line.create_date or ''
            ),
            'invoice_qty': line.invoice_qty,
            'invoice_price': line.invoice_price,
            'subtotal': line.subtotal,
            'purchase_order_line_id': line.purchase_order_line_id.id or 0,
        }
        DecisionLog.create({
            'line_id': line.id,
            'decision': decision,
            'reason': reason,
            'evidence_fingerprint': _legacy_fingerprint(
                'line-decision', values,
            ),
            'decided_by_id': values['actor_id'],
            'decided_at': line.overridden_at or line.write_date or line.create_date,
        })
        decision_logs += 1

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

    cr.execute(
        """
        SELECT COUNT(*)
          FROM account_move
         WHERE eh_payment_hold IS TRUE
           AND NOT EXISTS (
               SELECT 1
                 FROM eh_ap_payment_hold_log AS log
                WHERE log.move_id = account_move.id
           )
        """,
    )
    legacy_holds = cr.fetchone()[0]

    _logger.warning(
        "AP automation 19.0.1.4.1: linked/sealed %d unambiguous legacy "
        "move(s); %d conflicting move link(s) left unclaimed; copied %d "
        "legacy duplicate override(s) and %d line decision(s) into labelled "
        "append-only observations; quarantined %d live intake(s) and reset "
        "%d line(s) for re-match. %d legacy payment hold(s) lack "
        "reconstructible event history and remain visibly held without a "
        "fabricated log row.",
        linked_moves, conflicting_moves, duplicate_logs, decision_logs,
        quarantined_intakes, reset_lines, legacy_holds,
    )
