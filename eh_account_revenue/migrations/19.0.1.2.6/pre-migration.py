# -*- encoding: utf-8 -*-
"""Fail closed on legacy revenue anchors and child audit evidence.

Before durable server ownership, financial anchors, modification rows,
constraint reviews, allocation freezes, and stored related scope were all
RPC-writable.  Their shape cannot prove provenance.  Preserve every row and
quarantine each affected contract before related-field recomputation can
erase a scope mismatch.
"""

import logging


_logger = logging.getLogger(__name__)

_REQUIRED_SCHEMA = {
    'eh_revenue_contract': {
        'id', 'company_id', 'currency_id', 'amount_recognised',
        'amount_billed', 'financing_interest_recognised',
        'close_released_amount', 'eh_integrity_review_required',
        'eh_integrity_review_reason',
    },
    'account_move': {'eh_revenue_contract_id'},
    'eh_revenue_modification': {'contract_id'},
    'eh_revenue_constraint_review': {'contract_id'},
    'eh_revenue_obligation': {
        'contract_id', 'recognised_amount', 'allocation_frozen',
        'frozen_allocation', 'company_id', 'currency_id',
    },
}


def _schema_ready(cr):
    cr.execute(
        """
        SELECT table_name, column_name
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = ANY(%s)
        """,
        (list(_REQUIRED_SCHEMA),),
    )
    available = {}
    for table, column in cr.fetchall():
        available.setdefault(table, set()).add(column)
    return all(
        columns <= available.get(table, set())
        for table, columns in _REQUIRED_SCHEMA.items()
    )


def migrate(cr, version):
    if not version:
        return
    # Odoo executes pre-migrations before ORM schema creation.  Upgrades from
    # releases older than 1.2.4 do not yet have several referenced tables and
    # columns.  Never make those upgrades fail: defer the same idempotent audit
    # to 1.2.7 post-migration, after registry/schema update.
    if not _schema_ready(cr):
        _logger.warning(
            "Revenue 1.2.6 legacy audit deferred until 1.2.7 post-migration "
            "because the source schema predates required evidence columns."
        )
        return
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
                   OR
                   COALESCE(o.allocation_frozen, FALSE)
                   OR ABS(COALESCE(o.frozen_allocation, 0.0)) > 0.000001
                   OR o.company_id IS DISTINCT FROM c.company_id
                   OR o.currency_id IS DISTINCT FROM c.currency_id
               )
         )
    """)
