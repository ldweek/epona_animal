# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Quarantine legacy posted batches without an approval-time source hash.

There is no defensible way to reconstruct what recipient bank/name and
originator values an approver saw before this release.  Backfilling today's
mutable values would manufacture approval evidence.  Leave those rows NULL so
the runtime guard fails closed; draft batches get a real fingerprint on their
next confirmation.  Log the population needing controlled review/migration.
"""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        SELECT COUNT(*)
          FROM eh_batch_payment AS batch
         WHERE batch.batch_type = 'outbound'
           AND batch.state IN ('confirmed', 'posted')
           AND COALESCE(batch.sepa_ct_source_fingerprint, '') = ''
           AND EXISTS (
               SELECT 1
                 FROM eh_sepa_originator AS originator
                WHERE originator.journal_id = batch.journal_id
           )
        """,
    )
    quarantined = cr.fetchone()[0]
    if quarantined:
        _logger.warning(
            "eh_account_sepa_ct 19.0.1.2.0: %d legacy approved/posted "
            "batches have no reconstructible approval-time source "
            "fingerprint and remain fail-closed for SEPA export.",
            quarantined,
        )
