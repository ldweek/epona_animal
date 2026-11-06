# -*- encoding: utf-8 -*-
"""Tie derived contract positions to preserved GL and quarantine conflicts."""


def migrate(cr, version):
    if not version:
        return

    # The link was ordinary RPC-writable before 1.2.4.  No legacy row carries
    # a trustworthy server capability stamp, so preserve every move/link but
    # quarantine every affected contract.  A controlled review can establish
    # provenance; migration must never manufacture it from a self-supplied FK.
    cr.execute("""
        UPDATE eh_revenue_contract c
           SET eh_integrity_review_required = TRUE,
               eh_integrity_review_reason = COALESCE(
                   NULLIF(c.eh_integrity_review_reason, ''),
                   'Legacy revenue journal links predate engine-owned '
                   'provenance and require controlled review.'
               )
         WHERE EXISTS (
             SELECT 1 FROM account_move am
              WHERE am.eh_revenue_contract_id = c.id
         )
    """)

    # Rebuild derived presentation only from posted same-company lines.
    # Never fabricate, rewrite, or relink journal evidence. Foreign legacy
    # links remain preserved but cannot poison contract position.
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

    # Old code could close with an unbilled asset or cancel after a
    # financing-only posting. Preserve terminal state and GL; mark conflict
    # explicitly for audited correction instead of silently reopening it.
    cr.execute("""
        WITH evidence AS (
            SELECT c.id,
                   COALESCE(SUM(aml.balance) FILTER (
                       WHERE am.state = 'posted'
                         AND am.company_id = c.company_id
                         AND aml.account_id IN (
                             c.contract_asset_account_id,
                             c.contract_liability_account_id
                         )
                   ), 0.0) AS net,
                   BOOL_OR(
                       am.state = 'posted'
                       AND am.company_id = c.company_id
                   ) AS has_posted
              FROM eh_revenue_contract c
              LEFT JOIN account_move am
                ON am.eh_revenue_contract_id = c.id
              LEFT JOIN account_move_line aml
                ON aml.move_id = am.id
             GROUP BY c.id
        )
        UPDATE eh_revenue_contract c
           SET eh_integrity_review_required = TRUE,
               eh_integrity_review_reason = COALESCE(
                   NULLIF(c.eh_integrity_review_reason, ''),
                   'Legacy terminal state conflicts with preserved posted '
                   'revenue/financing contract-position evidence.'
               )
          FROM evidence e
         WHERE e.id = c.id
           AND (
               (c.state = 'done' AND ABS(e.net) > 0.000001)
               OR (c.state = 'cancelled' AND COALESCE(e.has_posted, FALSE))
               OR (
                   NOT COALESCE(e.has_posted, FALSE)
                   AND (
                       ABS(COALESCE(c.amount_recognised, 0.0)) > 0.000001
                       OR ABS(COALESCE(c.amount_billed, 0.0)) > 0.000001
                       OR ABS(COALESCE(
                           c.financing_interest_recognised, 0.0
                       )) > 0.000001
                       OR ABS(COALESCE(
                           c.close_released_amount, 0.0
                       )) > 0.000001
                   )
               )
           )
    """)
