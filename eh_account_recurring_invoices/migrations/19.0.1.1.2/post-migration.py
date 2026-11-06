# -*- encoding: utf-8 -*-
"""Backfill durable service-period keys and quarantine ambiguous history."""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # A missing service-period anchor or two non-cancelled generations for
    # one template/period cannot be made unique honestly. Preserve every row
    # and foreign key, but exclude that history from verified engine evidence
    # and stop the affected template for manager review.
    cr.execute("""
        WITH ambiguous_templates AS (
            SELECT DISTINCT am.eh_recurring_template_id AS template_id
              FROM account_move am
             WHERE am.eh_recurring_template_id IS NOT NULL
               AND am.state <> 'cancel'
               AND (
                    am.eh_recurring_period_start IS NULL
                    OR EXISTS (
                        SELECT 1
                          FROM account_move sibling
                         WHERE sibling.eh_recurring_template_id =
                               am.eh_recurring_template_id
                           AND sibling.eh_recurring_period_start =
                               am.eh_recurring_period_start
                           AND sibling.state <> 'cancel'
                           AND sibling.id <> am.id
                    )
               )
        )
        UPDATE eh_recurring_invoice_template template
           SET eh_legacy_integrity_unverified = TRUE,
               eh_legacy_integrity_reason = COALESCE(
                   NULLIF(template.eh_legacy_integrity_reason, ''),
                   'Upgrade found missing or duplicate active recurring '
                   'service-period evidence; historical rows were preserved '
                   'and quarantined instead of inventing an idempotency key.'
               ),
               state = CASE
                   WHEN template.state = 'active' THEN 'paused'
                   ELSE template.state
               END
          FROM ambiguous_templates ambiguous
         WHERE template.id = ambiguous.template_id
    """)
    template_count = cr.rowcount

    cr.execute("""
        UPDATE account_move move
           SET eh_recurring_legacy_unverified = TRUE,
               eh_legacy_unverified_seal = TRUE,
               eh_sealed = FALSE,
               eh_recurring_generation_key = NULL
          FROM eh_recurring_invoice_template template
         WHERE move.eh_recurring_template_id = template.id
           AND template.eh_legacy_integrity_unverified = TRUE
    """)
    quarantined_move_count = cr.rowcount

    cr.execute("""
        UPDATE account_move move
           SET eh_recurring_generation_key =
               move.eh_recurring_template_id::text || ':' ||
               move.eh_recurring_period_start::text
          FROM eh_recurring_invoice_template template
         WHERE move.eh_recurring_template_id = template.id
           AND move.state <> 'cancel'
           AND move.eh_recurring_period_start IS NOT NULL
           AND NOT template.eh_legacy_integrity_unverified
           AND NOT move.eh_recurring_legacy_unverified
           AND move.eh_recurring_generation_key IS NULL
    """)
    backfilled_count = cr.rowcount

    _logger.warning(
        "Recurring period-key upgrade backfilled %s move(s), quarantined %s "
        "ambiguous template(s) and %s linked move(s).",
        backfilled_count,
        template_count,
        quarantined_move_count,
    )
