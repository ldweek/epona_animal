# -*- encoding: utf-8 -*-
"""Release inbox rows falsely quarantined by the 1.4.3 provenance audit."""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # Safe recovery is intentionally narrow.  A non-posted row with no move,
    # no reverse link and no posting actor/time never claimed posted
    # provenance.  Preserve every quarantine that has any posting evidence;
    # those still require a human review and must not be auto-repaired.
    cr.execute(
        """
        UPDATE eh_ap_intake AS intake
           SET provenance_quarantined = FALSE,
               provenance_quarantine_note =
                   '19.0.1.4.5 remediation: false inbox quarantine from '
                   '19.0.1.4.3 cleared. No posting provenance was claimed; '
                   'the operational intake was preserved.',
               write_date = NOW()
         WHERE intake.provenance_quarantined IS TRUE
           AND intake.provenance_quarantine_note LIKE
               '19.0.1.4.3 upgrade audit:%'
           AND intake.state != 'posted'
           AND intake.move_id IS NULL
           AND intake.posted_at IS NULL
           AND intake.posted_by_id IS NULL
           AND NOT EXISTS (
                SELECT 1
                  FROM account_move AS move
                 WHERE move.eh_ap_intake_id = intake.id
           )
        """,
    )
    _logger.warning(
        "AP automation 19.0.1.4.5 released %d false inbox "
        "provenance quarantine(s).",
        cr.rowcount,
    )
