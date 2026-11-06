# -*- coding: utf-8 -*-
"""Backfill explicit SEPA routing and attachment evidence markers."""


def migrate(cr, version):
    cr.execute(
        """
        UPDATE eh_batch_payment
           SET sepa_ct_enabled = TRUE
         WHERE COALESCE(sepa_ct_source_fingerprint, '') <> ''
           AND NOT COALESCE(sepa_ct_enabled, FALSE)
        """
    )
    cr.execute(
        """
        UPDATE ir_attachment attachment
           SET eh_sepa_ct_export_evidence = TRUE
          FROM eh_sepa_export export
         WHERE export.attachment_id = attachment.id
           AND NOT COALESCE(attachment.eh_sepa_ct_export_evidence, FALSE)
        """
    )
