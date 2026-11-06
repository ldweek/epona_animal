# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Quarantine every export whose generator provenance predates this field.

Before 19.0.1.2.1, model ACLs and workflow guards did not constitute proof
that an existing row and its attachment were produced by the PAIN generator.
There is no trustworthy historical fact from which to reconstruct that proof.
Keep every row, attachment, hash, state, actor and timestamp exactly as stored,
and mark only the new provenance label fail-closed.
"""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        UPDATE eh_sepa_export
           SET provenance_status = 'legacy_unverified'
         WHERE provenance_status IS DISTINCT FROM 'legacy_unverified'
        """,
    )
    quarantined = cr.rowcount
    if quarantined:
        _logger.warning(
            "eh_account_sepa_ct 19.0.1.2.1: quarantined %d pre-control "
            "SEPA export rows without changing their XML, hash, state, "
            "actor or timestamp.",
            quarantined,
        )
