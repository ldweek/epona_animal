# -*- coding: utf-8 -*-
"""Quarantine unprovable legacy origins/policies; retain all GL evidence."""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # Only bind a legacy Generated Asset pointer back onto the asset when the
    # pair is unique and every exact source invariant can be re-proved. Never
    # guess among multiple invoice lines or merely match on amount/name.
    cr.execute(
        """
        CREATE TEMP TABLE eh_asset_valid_origin ON COMMIT DROP AS
        WITH candidates AS (
            SELECT line.id AS line_id,
                   line.eh_asset_id AS asset_id,
                   count(*) OVER (PARTITION BY line.eh_asset_id) AS asset_links
              FROM account_move_line AS line
             WHERE line.eh_asset_id IS NOT NULL
        )
        SELECT candidate.line_id, candidate.asset_id
          FROM candidates AS candidate
          JOIN account_move_line AS line ON line.id = candidate.line_id
          JOIN account_move AS bill ON bill.id = line.move_id
          JOIN eh_asset AS asset ON asset.id = candidate.asset_id
          JOIN account_account AS account ON account.id = line.account_id
          JOIN res_currency AS currency
            ON currency.id = (SELECT company.currency_id
                                FROM res_company AS company
                               WHERE company.id = asset.company_id)
         WHERE candidate.asset_links = 1
           AND bill.move_type = 'in_invoice'
           AND bill.state = 'posted'
           AND bill.id = asset.invoice_id
           AND bill.company_id = asset.company_id
           AND line.company_id = asset.company_id
           AND (line.display_type IS NULL OR line.display_type = 'product')
           AND account.eh_asset_category_id = asset.category_id
           AND abs(COALESCE(line.balance, 0)
                   - COALESCE(asset.acquisition_cost, 0))
               <= COALESCE(currency.rounding, 0.01) / 2.0
        """,
    )
    cr.execute(
        """
        UPDATE eh_asset AS asset
           SET invoice_line_id = valid.line_id,
               write_date = NOW()
          FROM eh_asset_valid_origin AS valid
         WHERE asset.id = valid.asset_id
           AND asset.invoice_line_id IS NULL
        """,
    )
    linked = cr.rowcount
    cr.execute(
        """
        CREATE TEMP TABLE eh_asset_invalid_origin ON COMMIT DROP AS
        SELECT DISTINCT line.eh_asset_id AS asset_id
          FROM account_move_line AS line
         WHERE line.eh_asset_id IS NOT NULL
           AND NOT EXISTS (
                SELECT 1
                  FROM eh_asset_valid_origin AS valid
                 WHERE valid.line_id = line.id
                   AND valid.asset_id = line.eh_asset_id
           )
        """,
    )
    cr.execute(
        """
        UPDATE eh_asset AS asset
           SET origin_source_quarantined = TRUE,
               origin_source_quarantine_note =
                   '19.0.1.5.2 upgrade audit: a legacy Generated Asset link '
                   'could not be proven from a unique exact bill line, '
                   'posted same-company bill, category and company-currency '
                   'cost. The ambiguous pointer was cleared; no journal '
                   'entry or asset evidence was deleted.',
               write_date = NOW()
          FROM eh_asset_invalid_origin AS invalid
         WHERE asset.id = invalid.asset_id
        """,
    )
    quarantined_origins = cr.rowcount
    cr.execute(
        """
        UPDATE account_move_line AS line
           SET eh_asset_id = NULL,
               write_date = NOW()
          FROM eh_asset_invalid_origin AS invalid
         WHERE line.eh_asset_id = invalid.asset_id
        """,
    )

    # A posted legacy annual row does not prove which editable pool policy was
    # in force at posting time. Preserve it and mark the missing basis; do not
    # copy today's rates/accounts backwards. Draft rows can safely adopt the
    # current locked policy because they have not booked any evidence yet.
    cr.execute(
        """
        UPDATE eh_asset_lvp_pool_line AS line
           SET policy_quarantined = TRUE,
               policy_quarantine_note =
                   '19.0.1.5.2 upgrade audit: the legacy posted row predates '
                   'immutable rate/account snapshots. Existing GL evidence '
                   'was retained; no historical policy was invented.',
               first_year_rate = NULL,
               subsequent_year_rate = NULL,
               pool_account_id = NULL,
               accumulated_account_id = NULL,
               expense_account_id = NULL,
               journal_id = NULL
         WHERE line.is_posted IS TRUE OR line.move_id IS NOT NULL
        """,
    )
    quarantined_pool_lines = cr.rowcount
    cr.execute(
        """
        UPDATE eh_asset_lvp_pool_line AS line
           SET first_year_rate = pool.first_year_rate,
               subsequent_year_rate = pool.subsequent_year_rate,
               pool_account_id = pool.pool_account_id,
               accumulated_account_id = pool.accumulated_account_id,
               expense_account_id = pool.expense_account_id,
               journal_id = pool.journal_id
          FROM eh_asset_lvp_pool AS pool
         WHERE pool.id = line.pool_id
           AND NOT line.is_posted
           AND line.move_id IS NULL
        """,
    )

    # Pre-1.5.2 outcomes are real historical rows, but they cannot be upgraded
    # into an event snapshot without fabricating the old member/input basis.
    cr.execute(
        """
        UPDATE eh_asset_cgu AS cgu
           SET legacy_test_basis_quarantined = TRUE,
               legacy_test_basis_note =
                   '19.0.1.5.2 upgrade audit: legacy IAS 36 outcome retained '
                   'without inventing a historical member/cash-flow/rate '
                   'snapshot. Prospective tests create immutable events.',
               write_date = NOW()
         WHERE cgu.last_test_date IS NOT NULL
            OR cgu.last_test_result IS NOT NULL
            OR EXISTS (
                SELECT 1 FROM eh_asset_impairment AS impairment
                 WHERE impairment.cgu_id = cgu.id
                   AND (impairment.state IN ('posted', 'cancelled')
                        OR impairment.move_id IS NOT NULL
                        OR impairment.reversal_move_id IS NOT NULL)
            )
        """,
    )

    # Complete the currency-evidence closure omitted by 1.5.1: explicit AUC,
    # revaluation, pool-transfer and lease-modification links all constitute
    # GL evidence even when their convenience stamps/counters are missing.
    cr.execute(
        """
        UPDATE eh_asset AS asset
           SET currency_mismatch_quarantined = TRUE,
               currency_mismatch_note =
                   '19.0.1.5.2 upgrade audit: asset currency differs from '
                   'company currency and explicit linked ledger evidence '
                   'exists. History was retained without reinterpretation.',
               write_date = NOW()
          FROM res_company AS company
         WHERE company.id = asset.company_id
           AND asset.currency_id <> company.currency_id
           AND NOT asset.currency_mismatch_quarantined
           AND (
                asset.capitalisation_move_id IS NOT NULL
                OR asset.lvp_transfer_move_id IS NOT NULL
                OR EXISTS (
                    SELECT 1 FROM eh_asset_revaluation_move_rel AS rel
                     WHERE rel.asset_id = asset.id
                )
           )
        """,
    )
    cr.execute(
        """
        UPDATE eh_lease_contract AS lease
           SET currency_mismatch_quarantined = TRUE,
               currency_mismatch_note =
                   '19.0.1.5.2 upgrade audit: lease currency differs from '
                   'company currency and an explicit modification move '
                   'exists. History was retained without reinterpretation.',
               write_date = NOW()
          FROM res_company AS company
         WHERE company.id = lease.company_id
           AND lease.currency_id <> company.currency_id
           AND NOT lease.currency_mismatch_quarantined
           AND EXISTS (
                SELECT 1 FROM eh_lease_modification_move_rel AS rel
                 WHERE rel.lease_id = lease.id
           )
        """,
    )
    _logger.warning(
        "Assets Pro 19.0.1.5.2 linked %d proven bill origins, quarantined "
        "%d ambiguous origins and %d posted legacy pool policy bases.",
        linked, quarantined_origins, quarantined_pool_lines,
    )
