# -*- coding: utf-8 -*-
"""Quarantine legacy LVP rows whose fiscal allocation basis is unproved."""

import logging


_logger = logging.getLogger(__name__)


def _quarantine_legacy_lvp_lines(cr):
    """Snapshot and quarantine each pre-fix row without changing its values.

    The old model persisted only aggregate results, not the member set used
    to calculate them. Reconstructing a historical fiscal-year allocation is
    therefore not provable. The reviewer workflow can recompute only a wholly
    unposted row from the still-live source assets; posted rows remain intact.

    ``allocation_basis_original_values IS NULL`` makes this idempotent and
    prevents a manager-resolved row (which retains its snapshot) being
    quarantined again if the helper is retried.
    """
    cr.execute(
        """
        UPDATE eh_asset_lvp_pool_line AS line
           SET allocation_basis_quarantined = TRUE,
               allocation_basis_quarantine_note =
                   CASE
                     WHEN line.is_posted IS TRUE OR line.move_id IS NOT NULL
                     THEN '19.0.1.5.7 upgrade audit: this posted legacy row '
                          'was calculated before pool allocations followed '
                          'the company financial year. Journal and row '
                          'evidence were retained byte-for-byte; review and '
                          'correct through a separate General Ledger '
                          'adjustment if required.'
                     ELSE '19.0.1.5.7 upgrade audit: this unposted legacy row '
                          'was calculated before pool allocations followed '
                          'the company financial year. It was not rewritten; '
                          'a manager must recompute it from current source '
                          'assets before posting.'
                   END,
               allocation_basis_original_values =
                   jsonb_build_object(
                       'year', line.year,
                       'opening_balance', line.opening_balance,
                       'additions', line.additions,
                       'amount', line.amount,
                       'first_year_rate', line.first_year_rate,
                       'subsequent_year_rate', line.subsequent_year_rate,
                       'pool_account_id', line.pool_account_id,
                       'accumulated_account_id', line.accumulated_account_id,
                       'expense_account_id', line.expense_account_id,
                       'journal_id', line.journal_id,
                       'is_posted', line.is_posted,
                       'move_id', line.move_id
                   )::text,
               write_date = NOW()
         WHERE line.allocation_basis_original_values IS NULL
        """,
    )
    return cr.rowcount


def migrate(cr, version):
    if not version:
        return
    quarantined = _quarantine_legacy_lvp_lines(cr)
    _logger.warning(
        "Assets Pro 19.0.1.5.7 quarantined %d legacy low-value-pool "
        "annual rows without rewriting any source, result, or journal "
        "evidence.",
        quarantined,
    )
