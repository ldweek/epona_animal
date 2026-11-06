# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Backfill debtor names and quarantine unprovable legacy approval evidence.

The current partner name is the only recoverable label for old mandates and is
sealed as a best-effort legacy snapshot.  Approval-time creditor/mandate hashes
and per-payment consumption decisions cannot be reconstructed honestly, so
legacy approved/exported batches remain NULL and fail closed for a new cut or
re-cut until a controlled review/migration establishes evidence.
"""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        UPDATE eh_sepa_mandate AS mandate
           SET debtor_name = COALESCE(
                   NULLIF(BTRIM(partner.complete_name), ''),
                   NULLIF(BTRIM(partner.name), '')
               )
          FROM res_partner AS partner
         WHERE partner.id = mandate.partner_id
           AND COALESCE(BTRIM(mandate.debtor_name), '') = ''
        """,
    )
    _logger.info(
        "eh_account_sepa_dd 19.0.1.1.0: backfilled %d legacy debtor-name "
        "snapshots.",
        cr.rowcount,
    )
    cr.execute(
        """
        SELECT COUNT(*)
          FROM eh_batch_payment AS batch
         WHERE batch.batch_type = 'inbound'
           AND batch.state IN ('confirmed', 'posted')
           AND COALESCE(batch.sepa_dd_source_fingerprint, '') = ''
           AND EXISTS (
               SELECT 1
                 FROM eh_sepa_creditor AS creditor
                WHERE creditor.journal_id = batch.journal_id
                  AND creditor.company_id = batch.company_id
           )
        """,
    )
    quarantined = cr.fetchone()[0]
    if quarantined:
        _logger.warning(
            "eh_account_sepa_dd 19.0.1.1.0: %d legacy approved/posted "
            "batches lack reconstructible creditor/mandate approval hashes "
            "and remain fail-closed for new exports.",
            quarantined,
        )
