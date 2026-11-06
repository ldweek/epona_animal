# -*- encoding: utf-8 -*-
"""Quarantine AP links whose legacy provenance cannot be proved."""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # The 1.4.1 migration used uniqueness of intake.move_id as its only proof.
    # Build a conservative set of invalid links before clearing either side.
    cr.execute(
        """
        CREATE TEMP TABLE eh_ap_invalid_provenance ON COMMIT DROP AS
        SELECT DISTINCT intake.id AS intake_id, move.id AS move_id
          FROM eh_ap_intake AS intake
          FULL JOIN account_move AS move
            ON move.eh_ap_intake_id = intake.id
            OR intake.move_id = move.id
          LEFT JOIN res_partner AS intake_partner
            ON intake_partner.id = intake.partner_id
          LEFT JOIN res_partner AS move_partner
            ON move_partner.id = move.partner_id
          LEFT JOIN res_currency AS currency
            ON currency.id = intake.currency_id
         WHERE intake.id IS NOT NULL
           -- A live inbox row does not claim journal-entry provenance.  The
           -- old migration treated the absence of a move as failed proof and
           -- quarantined every received/parsed/matched/exception/rejected
           -- intake.  Audit only rows that claim a posting or a posting link.
           AND (
                intake.state = 'posted'
                OR intake.move_id IS NOT NULL
                OR move.eh_ap_intake_id IS NOT NULL
                OR intake.posted_at IS NOT NULL
                OR intake.posted_by_id IS NOT NULL
           )
           AND (
                move.id IS NULL
                OR intake.move_id IS DISTINCT FROM move.id
                OR move.eh_ap_intake_id IS DISTINCT FROM intake.id
                OR intake.state != 'posted'
                OR move.state != 'posted'
                OR move.move_type != 'in_invoice'
                OR move.company_id IS DISTINCT FROM intake.company_id
                OR move.currency_id IS DISTINCT FROM intake.currency_id
                OR intake_partner.commercial_partner_id IS DISTINCT FROM
                   move_partner.commercial_partner_id
                OR intake.posted_at IS NULL
                OR intake.posted_by_id IS NULL
                OR length(COALESCE(intake.source_fingerprint, '')) != 64
                OR NOT EXISTS (
                    SELECT 1 FROM eh_ap_intake_line AS line
                     WHERE line.intake_id = intake.id
                )
                OR abs(COALESCE(move.amount_total, 0)
                       - COALESCE(intake.extracted_total, 0))
                   > COALESCE(currency.rounding, 0.01) / 2.0
           )
        """,
    )
    cr.execute(
        """
        UPDATE eh_ap_intake AS intake
           SET provenance_quarantined = TRUE,
               provenance_quarantine_note =
                   '19.0.1.4.3 upgrade audit: the legacy journal-entry link '
                   'could not be proven from mutual posted-link, company, '
                   'commercial vendor, currency, total, actor and captured '
                   'source evidence. The link was cleared without deleting '
                   'either record.',
               move_id = NULL,
               write_date = NOW()
          FROM eh_ap_invalid_provenance AS invalid
         WHERE intake.id = invalid.intake_id
        """,
    )
    quarantined = cr.rowcount
    cr.execute(
        """
        UPDATE account_move AS move
           SET eh_ap_intake_id = NULL,
               write_date = NOW()
          FROM eh_ap_invalid_provenance AS invalid
         WHERE move.id = invalid.move_id
           AND move.eh_ap_intake_id IS NOT NULL
        """,
    )
    _logger.warning(
        "AP automation 19.0.1.4.3 quarantined %d unproven legacy AP "
        "provenance link(s); journal entries and seal state were preserved.",
        quarantined,
    )
