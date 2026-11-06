# -*- encoding: utf-8 -*-
"""Quarantine all indistinguishable legacy recurring evidence.

Before 1.0.10, move links, workflow state, counters, and period anchors had no
durable provenance version. A structurally plausible row cannot prove that it
came from the engine. Preserve every row and relationship, but make every
affected template immutable and every affected move an unverified frozen
source.
"""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        WITH affected AS (
            SELECT t.id
              FROM eh_recurring_invoice_template t
             WHERE t.state <> 'draft'
                OR COALESCE(t.count_generated, 0) <> 0
                OR t.last_run_at IS NOT NULL
                OR t.last_generated_move_id IS NOT NULL
                OR NULLIF(t.last_error, '') IS NOT NULL
                OR EXISTS (
                    SELECT 1
                      FROM account_move am
                     WHERE am.eh_recurring_template_id = t.id
                        OR am.eh_recurring_proration_template_id = t.id
                )
        )
        UPDATE eh_recurring_invoice_template t
           SET eh_legacy_integrity_unverified = TRUE,
               eh_legacy_integrity_reason = COALESCE(
                   NULLIF(t.eh_legacy_integrity_reason, ''),
                   'Legacy recurring workflow, counters, service-period '
                   'anchors, or move links predate durable engine provenance.'
               )
          FROM affected a
         WHERE t.id = a.id
    """)
    template_count = cr.rowcount

    cr.execute("""
        WITH affected_moves AS (
            SELECT am.id
              FROM account_move am
             WHERE am.eh_recurring_template_id IS NOT NULL
                OR am.eh_recurring_proration_template_id IS NOT NULL
                OR am.eh_recurring_proration_source_move_id IS NOT NULL
                OR EXISTS (
                    SELECT 1
                      FROM eh_recurring_invoice_template t
                     WHERE t.last_generated_move_id = am.id
                )
        )
        UPDATE account_move am
           SET eh_recurring_legacy_unverified = TRUE,
               eh_legacy_unverified_seal = TRUE,
               eh_sealed = FALSE
          FROM affected_moves affected
         WHERE am.id = affected.id
    """)
    move_count = cr.rowcount
    _logger.warning(
        "Quarantined %s legacy recurring template(s) and %s linked move(s); "
        "all rows and foreign keys were preserved.",
        template_count,
        move_count,
    )
