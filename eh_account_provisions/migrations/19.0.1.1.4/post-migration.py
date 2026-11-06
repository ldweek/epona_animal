# -*- encoding: utf-8 -*-
"""Rebuild derived anchors and quarantine unverifiable provision journals."""


def migrate(cr, version):
    if not version:
        return

    # Stored related/computed fields were historically RPC-writable despite
    # being readonly in the client.  Rebuild them from their server-owned
    # inputs before the new ORM guard takes ownership.
    cr.execute("""
        UPDATE eh_provision provision
           SET currency_id = company.currency_id,
               present_value = (
                   ROUND((
                       CASE
                           WHEN COALESCE(provision.discount_rate, 0.0) != 0.0
                            AND COALESCE(provision.periods_to_settlement, 0) != 0
                           THEN provision.best_estimate / POWER(
                               1.0 + provision.discount_rate / 100.0,
                               provision.periods_to_settlement
                           )
                           ELSE provision.best_estimate
                       END / currency.rounding
                   )::numeric) * currency.rounding
               )
          FROM res_company company
          JOIN res_currency currency ON currency.id = company.currency_id
         WHERE company.id = provision.company_id
    """)

    # A legacy link is accepted only when the linked move has the full shape
    # produced by the provision engine: exact company/journal/name, posted and
    # sealed, and every journal line routed through one of the provision's
    # configured accounts.  Anything else is preserved but quarantined so it
    # cannot feed deferred tax or another posting transition silently.
    cr.execute("""
        UPDATE eh_provision provision
           SET eh_integrity_review_required = TRUE,
               eh_integrity_review_reason = COALESCE(
                   NULLIF(provision.eh_integrity_review_reason, ''),
                   'Legacy provision journal provenance is missing, '
                   'cross-company, unsealed, or inconsistent with the '
                   'provision posting basis.'
               )
         WHERE EXISTS (
                   SELECT 1
                     FROM account_move move
                    WHERE move.eh_provision_id = provision.id
                      AND (
                          move.company_id IS DISTINCT FROM provision.company_id
                          OR move.state IS DISTINCT FROM 'posted'
                          OR move.eh_sealed IS NOT TRUE
                          OR move.move_type IS DISTINCT FROM 'entry'
                          OR move.journal_id IS DISTINCT FROM provision.journal_id
                          OR COALESCE(move.ref, '') IS DISTINCT FROM
                             COALESCE(provision.name, '')
                          OR (
                              SELECT COUNT(*)
                                FROM account_move_line line
                               WHERE line.move_id = move.id
                                 AND (line.debit != 0.0 OR line.credit != 0.0)
                          ) != 2
                          OR NOT EXISTS (
                              SELECT 1
                                FROM account_move_line debit_line
                                JOIN account_move_line credit_line
                                  ON credit_line.move_id = debit_line.move_id
                                 AND credit_line.id != debit_line.id
                               WHERE debit_line.move_id = move.id
                                 AND debit_line.debit > 0.0
                                 AND debit_line.credit = 0.0
                                 AND credit_line.credit > 0.0
                                 AND credit_line.debit = 0.0
                                 AND ABS(
                                     debit_line.debit - credit_line.credit
                                 ) < 0.000001
                                 AND (
                                     (
                                         debit_line.account_id =
                                             provision.expense_account_id
                                         AND credit_line.account_id =
                                             provision.provision_account_id
                                     ) OR (
                                         debit_line.account_id =
                                             provision.finance_cost_account_id
                                         AND credit_line.account_id =
                                             provision.provision_account_id
                                     ) OR (
                                         debit_line.account_id =
                                             provision.provision_account_id
                                         AND credit_line.account_id =
                                             provision.expense_account_id
                                     ) OR (
                                         debit_line.account_id =
                                             provision.provision_account_id
                                         AND credit_line.account_id =
                                             provision.settlement_account_id
                                     ) OR (
                                         debit_line.account_id =
                                             provision.asset_account_id
                                         AND credit_line.account_id =
                                             provision.income_account_id
                                     ) OR (
                                         debit_line.account_id =
                                             provision.income_account_id
                                         AND credit_line.account_id =
                                             provision.asset_account_id
                                     ) OR (
                                         debit_line.account_id =
                                             provision.reimbursement_account_id
                                         AND credit_line.account_id =
                                             provision.expense_account_id
                                     )
                                 )
                          )
                      )
               )
            OR (
                provision.state = 'draft'
                AND EXISTS (
                    SELECT 1 FROM account_move move
                     WHERE move.eh_provision_id = provision.id
                )
            )
            OR (
                provision.state IN (
                    'recognised', 'recognised_asset', 'settled', 'reversed'
                )
                AND NOT EXISTS (
                    SELECT 1 FROM account_move move
                     WHERE move.eh_provision_id = provision.id
                       AND move.state = 'posted'
                       AND move.eh_sealed IS TRUE
                )
            )
            OR EXISTS (
                SELECT 1
                  FROM account_account account
                 WHERE (
                     account.id = provision.provision_account_id
                     AND account.account_type NOT IN (
                         'liability_current', 'liability_non_current'
                     )
                 ) OR (
                     account.id = provision.expense_account_id
                     AND account.account_type NOT IN (
                         'expense', 'expense_direct_cost'
                     )
                 ) OR (
                     account.id = provision.finance_cost_account_id
                     AND account.account_type != 'expense'
                 ) OR (
                     account.id = provision.settlement_account_id
                     AND account.account_type NOT IN (
                         'asset_cash', 'liability_payable', 'asset_current'
                     )
                 ) OR (
                     account.id = provision.asset_account_id
                     AND account.account_type NOT IN (
                         'asset_receivable', 'asset_current',
                         'asset_non_current'
                     )
                 ) OR (
                     account.id = provision.income_account_id
                     AND account.account_type NOT IN ('income', 'income_other')
                 ) OR (
                     account.id = provision.reimbursement_account_id
                     AND account.account_type NOT IN (
                         'asset_receivable', 'asset_current',
                         'asset_non_current'
                     )
                 )
            )
    """)
