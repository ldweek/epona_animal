# -*- encoding: utf-8 -*-
"""Run deferred legacy revenue evidence audit after ORM schema creation."""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    # Every referenced field exists now.  Query remains idempotent and only
    # sets a review flag/reason; no contract, child, or journal evidence is
    # deleted or rewritten.
    cr.execute("""
        UPDATE eh_revenue_contract c
           SET eh_integrity_review_required = TRUE,
               eh_integrity_review_reason = COALESCE(
                   NULLIF(c.eh_integrity_review_reason, ''),
                   'Legacy revenue anchors or child evidence predate '
                   'durable engine provenance and require controlled review.'
               )
         WHERE (
             NOT EXISTS (
                 SELECT 1 FROM account_move am
                  WHERE am.eh_revenue_contract_id = c.id
             )
             AND (
                 ABS(COALESCE(c.amount_recognised, 0.0)) > 0.000001
                 OR ABS(COALESCE(c.amount_billed, 0.0)) > 0.000001
                 OR ABS(COALESCE(
                     c.financing_interest_recognised, 0.0
                 )) > 0.000001
                 OR ABS(COALESCE(c.close_released_amount, 0.0)) > 0.000001
             )
         )
         OR EXISTS (
             SELECT 1 FROM eh_revenue_modification m
             WHERE m.contract_id = c.id
         )
         OR EXISTS (
             SELECT 1 FROM eh_revenue_constraint_review r
             WHERE r.contract_id = c.id
         )
         OR EXISTS (
             SELECT 1 FROM eh_revenue_obligation o
             WHERE o.contract_id = c.id
               AND (
                   ABS(COALESCE(o.recognised_amount, 0.0)) > 0.000001
                   OR COALESCE(o.allocation_frozen, FALSE)
                   OR ABS(COALESCE(o.frozen_allocation, 0.0)) > 0.000001
                   OR o.company_id IS DISTINCT FROM c.company_id
                   OR o.currency_id IS DISTINCT FROM c.currency_id
               )
         )
    """)
    _logger.warning(
        "Revenue 1.2.7 reviewed legacy evidence and quarantined %d "
        "contract(s) requiring controlled provenance review.",
        cr.rowcount,
    )
