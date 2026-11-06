# -*- coding: utf-8 -*-
"""Repair phantom parallel-book posting flags from the pre-workflow UI.

Before 19.0.1.5.1 ``is_posted`` was an editable Boolean and no production
method could create ``move_id``.  A true flag without a move therefore never
represented ledger evidence.  Clear only that provably invalid state; retain
any manually linked move for human review rather than erasing provenance.
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE eh_asset_book_line
           SET is_posted = FALSE
         WHERE is_posted IS TRUE
           AND move_id IS NULL
    """)

    # Do not reinterpret historical debit/credit figures by silently changing
    # source currencies. Quarantine only mismatched records that already have
    # ledger evidence; untouched draft/unposted records can be corrected in
    # the UI after upgrade. Runtime guards make quarantined rows read-only and
    # block all further posting.
    cr.execute("""
        UPDATE eh_asset AS asset
           SET currency_mismatch_quarantined = TRUE,
               currency_mismatch_note = format(
                   '19.0.1.5.1 upgrade audit: source currency id %s differs '
                   'from company currency id %s and ledger evidence exists. '
                   'No historical amount or journal entry was rewritten.',
                   asset.currency_id, company.currency_id
               )
          FROM res_company AS company
         WHERE company.id = asset.company_id
           AND asset.currency_id <> company.currency_id
           AND (
                asset.disposal_move_id IS NOT NULL
                OR COALESCE(asset.revaluation_adjustment, 0) <> 0
                OR COALESCE(asset.revaluation_surplus, 0) <> 0
                OR COALESCE(asset.revaluation_pl_decrease, 0) <> 0
                OR EXISTS (
                    SELECT 1
                      FROM eh_asset_depreciation_line AS dep
                     WHERE dep.asset_id = asset.id
                       AND (dep.is_posted OR dep.move_id IS NOT NULL)
                )
                OR EXISTS (
                    SELECT 1
                      FROM eh_asset_book AS book
                      JOIN eh_asset_book_line AS line
                        ON line.book_id = book.id
                     WHERE book.asset_id = asset.id
                       AND (
                            line.is_posted
                            OR line.move_id IS NOT NULL
                            OR line.reversal_move_id IS NOT NULL
                       )
                )
                OR EXISTS (
                    SELECT 1
                      FROM eh_asset_impairment AS impairment
                     WHERE impairment.asset_id = asset.id
                       AND (
                            impairment.state = 'posted'
                            OR impairment.move_id IS NOT NULL
                       )
                )
                OR (
                    asset.capitalised_at IS NOT NULL
                    AND asset.auc_account_id IS NOT NULL
                    AND asset.asset_account_id IS NOT NULL
                    AND asset.journal_id IS NOT NULL
                )
                OR EXISTS (
                    SELECT 1
                      FROM eh_asset_lvp_pool AS pool
                     WHERE pool.id = asset.lvp_pool_id
                       AND pool.pool_account_id IS NOT NULL
                       AND pool.journal_id IS NOT NULL
                )
           )
    """)

    cr.execute("""
        UPDATE eh_lease_contract AS lease
           SET currency_mismatch_quarantined = TRUE,
               currency_mismatch_note = format(
                   '19.0.1.5.1 upgrade audit: source currency id %s differs '
                   'from company currency id %s and ledger evidence exists. '
                   'No historical amount or journal entry was rewritten.',
                   lease.currency_id, company.currency_id
               )
          FROM res_company AS company
         WHERE company.id = lease.company_id
           AND lease.currency_id <> company.currency_id
           AND (
                lease.opening_move_id IS NOT NULL
                OR lease.termination_move_id IS NOT NULL
                OR COALESCE(lease.modification_count, 0) > 0
                OR EXISTS (
                    SELECT 1
                      FROM eh_lease_schedule_line AS line
                     WHERE line.lease_id = lease.id
                       AND (line.is_posted OR line.move_id IS NOT NULL)
                )
           )
    """)

    cr.execute("""
        UPDATE eh_asset_cgu AS cgu
           SET currency_mismatch_quarantined = TRUE,
               currency_mismatch_note = format(
                   '19.0.1.5.1 upgrade audit: CGU currency id %s differs '
                   'from company currency id %s and IAS 36 test/posted '
                   'impairment evidence exists. No historical measurement '
                   'or journal entry was rewritten.',
                   cgu.currency_id, company.currency_id
               )
          FROM res_company AS company
         WHERE company.id = cgu.company_id
           AND cgu.currency_id <> company.currency_id
           AND (
                cgu.last_test_date IS NOT NULL
                OR cgu.last_test_result IS NOT NULL
                OR EXISTS (
                    SELECT 1
                      FROM eh_asset_impairment AS impairment
                     WHERE impairment.cgu_id = cgu.id
                       AND (
                            impairment.state = 'posted'
                            OR impairment.move_id IS NOT NULL
                       )
                )
           )
    """)
