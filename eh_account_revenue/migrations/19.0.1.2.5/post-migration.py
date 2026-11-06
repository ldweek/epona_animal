# -*- encoding: utf-8 -*-
"""Quarantine indistinguishable legacy revenue links left by early 1.2.4."""


def migrate(cr, version):
    if not version:
        return

    # Early 1.2.4 made future writes engine-owned but did not mark existing
    # FKs untrusted.  Rows created before and after that upgrade have no
    # durable provenance-version marker, so fail closed for every linked
    # contract. Preserve all accounting rows and identifiers.
    cr.execute("""
        UPDATE eh_revenue_contract c
           SET eh_integrity_review_required = TRUE,
               eh_integrity_review_reason = COALESCE(
                   NULLIF(c.eh_integrity_review_reason, ''),
                   'Revenue journal links have no durable provenance-version '
                   'marker after the 1.2.4 upgrade and require controlled '
                   'review.'
               )
         WHERE EXISTS (
             SELECT 1 FROM account_move am
              WHERE am.eh_revenue_contract_id = c.id
         )
    """)

    # Exclude foreign-company legacy links from derived figures even while
    # retaining them for investigation.
    cr.execute("""
        WITH positions AS (
            SELECT c.id,
                   COALESCE(SUM(aml.balance) FILTER (
                       WHERE am.state = 'posted'
                         AND am.company_id = c.company_id
                         AND aml.account_id IN (
                             c.contract_asset_account_id,
                             c.contract_liability_account_id
                         )
                   ), 0.0) AS net
              FROM eh_revenue_contract c
              LEFT JOIN account_move am
                ON am.eh_revenue_contract_id = c.id
              LEFT JOIN account_move_line aml
                ON aml.move_id = am.id
             GROUP BY c.id
        )
        UPDATE eh_revenue_contract c
           SET contract_asset = GREATEST(p.net, 0.0),
               contract_liability = GREATEST(-p.net, 0.0)
          FROM positions p
         WHERE p.id = c.id
    """)
