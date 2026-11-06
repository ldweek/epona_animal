# -*- encoding: utf-8 -*-
"""Quarantine every legacy recurring-to-revenue bridge anchor.

The old template/contract link and billing-registration bit were writable
without a durable provenance version. No legacy shape can distinguish a real
engine registration from a forged one. Keep all templates, moves, contracts,
counters, and amounts intact, while blocking both recurring and contract
actions pending controlled recreation/correction.
"""

import logging


_logger = logging.getLogger(__name__)


def _table_has_columns(cr, table_name, column_names):
    """Return False when a dependency is only partially upgraded.

    The bridge owns neither ``eh_revenue_contract`` nor its integrity-review
    columns.  A staged/partial upgrade must therefore skip that optional half
    of quarantine instead of aborting the whole registry update.
    """
    cr.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s "
        "AND column_name IN %s",
        (table_name, tuple(column_names)),
    )
    return {row[0] for row in cr.fetchall()} == set(column_names)


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        UPDATE eh_recurring_invoice_template t
           SET eh_legacy_integrity_unverified = TRUE,
               eh_legacy_integrity_reason = COALESCE(
                   NULLIF(t.eh_legacy_integrity_reason, ''),
                   'Legacy recurring-to-revenue contract linkage predates '
                   'durable bridge provenance and requires controlled review.'
               )
         WHERE t.revenue_contract_id IS NOT NULL
    """)
    template_count = cr.rowcount

    cr.execute("""
        WITH bridge_moves AS (
            SELECT am.id
              FROM account_move am
             WHERE COALESCE(am.eh_revenue_billing_registered, FALSE)
                OR (
                    am.eh_revenue_contract_id IS NOT NULL
                    AND (
                        am.eh_recurring_template_id IS NOT NULL
                        OR am.eh_recurring_proration_template_id IS NOT NULL
                        OR am.eh_recurring_proration_source_move_id IS NOT NULL
                    )
                )
        )
        UPDATE account_move am
           SET eh_recurring_legacy_unverified = TRUE,
               eh_legacy_unverified_seal = TRUE,
               eh_sealed = FALSE
          FROM bridge_moves affected
         WHERE am.id = affected.id
    """)
    move_count = cr.rowcount

    contract_count = 0
    contract_columns = {
        'id', 'eh_integrity_review_required', 'eh_integrity_review_reason',
    }
    if _table_has_columns(cr, 'eh_revenue_contract', contract_columns):
        cr.execute("""
            WITH affected_contracts AS (
                SELECT revenue_contract_id AS id
                  FROM eh_recurring_invoice_template
                 WHERE revenue_contract_id IS NOT NULL
                UNION
                SELECT am.eh_revenue_contract_id AS id
                  FROM account_move am
                 WHERE am.eh_revenue_contract_id IS NOT NULL
                   AND (
                       COALESCE(am.eh_revenue_billing_registered, FALSE)
                       OR am.eh_recurring_template_id IS NOT NULL
                       OR am.eh_recurring_proration_template_id IS NOT NULL
                       OR am.eh_recurring_proration_source_move_id IS NOT NULL
                   )
            )
            UPDATE eh_revenue_contract contract
               SET eh_integrity_review_required = TRUE,
                   eh_integrity_review_reason = COALESCE(
                       NULLIF(contract.eh_integrity_review_reason, ''),
                       'Legacy recurring billing bridge evidence predates '
                       'durable provenance and requires controlled review.'
                   )
              FROM affected_contracts affected
             WHERE contract.id = affected.id
        """)
        contract_count = cr.rowcount
    else:
        _logger.warning(
            "Skipped legacy revenue-contract quarantine because the revenue "
            "dependency is not yet at the integrity-review schema version; "
            "template and move quarantine completed independently."
        )

    _logger.warning(
        "Quarantined %s legacy bridge template(s), %s move(s), and %s "
        "contract(s); all accounting rows and anchors were preserved.",
        template_count,
        move_count,
        contract_count,
    )
