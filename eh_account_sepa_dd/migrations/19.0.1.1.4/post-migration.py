# -*- encoding: utf-8 -*-
"""Backfill explicit DD designation and one-shot amendment evidence."""


def migrate(cr, version):
    if not version:
        return

    # A captured fingerprint or export history is durable evidence that the
    # inbound batch was deliberately approved as a PAIN.008 collection.
    cr.execute("""
        UPDATE eh_batch_payment batch
           SET sepa_dd_collection = TRUE,
               sepa_dd_source_approved_at = COALESCE(
                   batch.sepa_dd_source_approved_at,
                   batch.confirmed_at
               ),
               sepa_dd_source_approved_by_id = COALESCE(
                   batch.sepa_dd_source_approved_by_id,
                   batch.confirmed_by_id
               )
         WHERE batch.batch_type = 'inbound'
           AND (
               COALESCE(batch.sepa_dd_source_fingerprint, '') <> ''
               OR EXISTS (
                   SELECT 1 FROM eh_sepa_dd_export export
                    WHERE export.batch_id = batch.id
               )
           )
    """)

    # If a proven engine amendment predates an existing export for a payment
    # that consumed that mandate, the bank file already carried the legacy
    # always-rendered block. Mark the earliest such payment as its first report
    # so the next new collection does not emit AmdmntInf again.
    cr.execute("""
        WITH first_report AS (
            SELECT DISTINCT ON (amendment.id)
                   amendment.id AS amendment_id,
                   payment.id AS payment_id,
                   export.generated_at AS reported_at
              FROM eh_sepa_mandate_amendment amendment
              JOIN account_payment payment
                ON payment.eh_sepa_dd_mandate_id = amendment.mandate_id
              JOIN eh_sepa_dd_export export
                ON export.batch_id = payment.eh_batch_payment_id
             WHERE COALESCE(amendment.engine_owned, FALSE)
               AND amendment.reported_payment_id IS NULL
               AND export.generated_at >= amendment.amended_at
             ORDER BY amendment.id, export.generated_at, payment.id
        )
        UPDATE eh_sepa_mandate_amendment amendment
           SET reported_payment_id = first_report.payment_id,
               reported_at = first_report.reported_at
          FROM first_report
         WHERE amendment.id = first_report.amendment_id
    """)
