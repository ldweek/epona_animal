# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Quarantine outbound transmissions that predate exact-byte pinning."""


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        UPDATE account_move
           SET eh_peppol_legacy_unpinned = TRUE
         WHERE eh_peppol_transmission_status IN ('queued', 'sent', 'delivered')
           AND (
               eh_peppol_payload_attachment_id IS NULL
               OR COALESCE(eh_peppol_payload_hash, '') = ''
           )
        """,
    )
