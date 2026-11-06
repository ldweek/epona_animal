# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Quarantine non-reconstructable IAS 40 opening/period/source evidence.

The former activation was state-only and the former cost depreciation action
had no immutable fiscal-period event. The upgrade never invents journal links,
dates, actors, or period snapshots. Exact inbound transfer entries are the one
safe exception: their immutable transfer row already identifies the opening
entry, so that existing evidence can be linked without inference.
"""


def _table_exists(cr, name):
    cr.execute("SELECT to_regclass(%s)", (name,))
    return bool(cr.fetchone()[0])


def migrate(cr, version):
    del version

    # An inbound transfer log already is exact opening evidence. Bind only a
    # unique, same-company, posted, sealed move linked to the same property.
    cr.execute(
        "UPDATE eh_investment_property prop SET opening_move_id = exact.move_id "
        "FROM ("
        " SELECT log.property_id, MIN(log.move_id) AS move_id "
        " FROM eh_investment_property_transfer log "
        " JOIN account_move move ON move.id = log.move_id "
        " JOIN eh_investment_property p ON p.id = log.property_id "
        " WHERE log.direction = 'in' AND move.state = 'posted' "
        " AND move.eh_sealed IS TRUE "
        " AND move.eh_investment_property_id = p.id "
        " AND move.company_id = p.company_id "
        " GROUP BY log.property_id HAVING COUNT(*) = 1"
        ") exact WHERE prop.id = exact.property_id "
        "AND prop.opening_move_id IS NULL"
    )

    # Every non-draft live/terminal property needs a ledger opening anchor.
    # A later remeasurement/depreciation/disposal move is not a substitute.
    cr.execute(
        "UPDATE eh_investment_property SET "
        "opening_evidence_quarantined = TRUE, "
        "opening_evidence_quarantine_note = %s "
        "WHERE state IN ('held', 'disposed', 'transferred') "
        "AND opening_move_id IS NULL",
        (
            "Legacy property entered the recognised lifecycle without an "
            "exact opening journal entry. No journal link was fabricated; "
            "historical records remain visible but further accounting is "
            "blocked pending an explicit correction.",
        ),
    )

    # Detect old cost-model depreciation by its exact property-linked posted
    # move and configured debit/credit accounts. No fiscal period, actor and
    # policy snapshot existed, so event rows cannot safely be manufactured.
    cr.execute(
        "UPDATE eh_investment_property prop SET "
        "depreciation_history_quarantined = TRUE, "
        "depreciation_history_quarantine_note = %s "
        "WHERE prop.model_basis = 'cost' AND EXISTS ("
        " SELECT 1 FROM account_move move "
        " JOIN account_move_line debit_line ON debit_line.move_id = move.id "
        " JOIN account_move_line credit_line ON credit_line.move_id = move.id "
        " WHERE move.eh_investment_property_id = prop.id "
        " AND move.state = 'posted' "
        " AND debit_line.account_id = prop.depreciation_expense_account_id "
        " AND debit_line.debit > 0 "
        " AND credit_line.account_id = prop.accumulated_depreciation_account_id "
        " AND credit_line.credit = debit_line.debit"
        ") AND NOT EXISTS ("
        " SELECT 1 FROM eh_investment_property_depreciation event "
        " WHERE event.property_id = prop.id"
        ")",
        (
            "Legacy depreciation journal entries have no immutable fiscal-"
            "period, policy-snapshot and actor rows. Existing GL evidence was "
            "preserved and no event history was fabricated.",
        ),
    )

    # Re-audit inbound ownership after the earlier terminalisation migration.
    # Mark exceptions instead of aborting the upgrade or silently rewriting
    # source evidence.
    cr.execute(
        "UPDATE eh_investment_property prop SET "
        "source_provenance_quarantined = TRUE, "
        "source_provenance_quarantine_note = %s "
        "WHERE EXISTS ("
        " SELECT 1 FROM eh_investment_property_transfer log "
        " LEFT JOIN eh_asset asset ON asset.id = log.source_asset_id "
        " LEFT JOIN account_move move ON move.id = log.move_id "
        " WHERE log.property_id = prop.id AND log.direction = 'in' "
        " AND (asset.id IS NULL OR asset.company_id != prop.company_id "
        " OR asset.disposal_move_id IS DISTINCT FROM log.move_id "
        " OR asset.lvp_pool_id IS NOT NULL "
        " OR move.id IS NULL OR move.state != 'posted' "
        " OR move.eh_sealed IS NOT TRUE "
        " OR move.eh_investment_property_id IS DISTINCT FROM prop.id)"
        ")",
        (
            "Legacy inbound source ownership or sealed journal provenance "
            "does not reconcile. The property and source were preserved and "
            "further accounting is blocked pending review.",
        ),
    )
    cr.execute(
        "UPDATE eh_investment_property prop SET "
        "source_provenance_quarantined = TRUE, "
        "source_provenance_quarantine_note = %s "
        "WHERE EXISTS ("
        " SELECT 1 FROM eh_investment_property_transfer mine "
        " JOIN eh_investment_property_transfer other "
        " ON other.source_asset_id = mine.source_asset_id "
        " AND other.id != mine.id AND other.direction = 'in' "
        " WHERE mine.property_id = prop.id AND mine.direction = 'in'"
        ")",
        (
            "The same fixed asset is claimed by multiple inbound transfer "
            "events. No owner was selected automatically.",
        ),
    )

    # Every known asset evidence stream dated after the transfer proves the
    # earlier source terminalisation was unsafe. These tables are dependencies
    # today, but guards keep the migration reusable on partially repaired DBs.
    evidence_queries = []
    if _table_exists(cr, 'eh_asset_depreciation_line'):
        evidence_queries.append(
            "SELECT line.asset_id, line.depreciation_date AS evidence_date "
            "FROM eh_asset_depreciation_line line WHERE line.is_posted IS TRUE"
        )
    if _table_exists(cr, 'eh_asset_impairment'):
        evidence_queries.append(
            "SELECT event.asset_id, event.impairment_date AS evidence_date "
            "FROM eh_asset_impairment event "
            "WHERE event.state IN ('posted', 'cancelled')"
        )
    if _table_exists(cr, 'eh_asset_book_line') and _table_exists(
            cr, 'eh_asset_book'):
        evidence_queries.append(
            "SELECT book.asset_id, line.depreciation_date AS evidence_date "
            "FROM eh_asset_book_line line "
            "JOIN eh_asset_book book ON book.id = line.book_id "
            "WHERE line.is_posted IS TRUE OR line.move_id IS NOT NULL "
            "OR line.reversal_move_id IS NOT NULL"
        )
    if _table_exists(cr, 'eh_asset_revaluation_move_rel'):
        evidence_queries.append(
            "SELECT rel.asset_id, move.date AS evidence_date "
            "FROM eh_asset_revaluation_move_rel rel "
            "JOIN account_move move ON move.id = rel.move_id "
            "WHERE move.state IN ('posted', 'cancel')"
        )
    if evidence_queries:
        union_sql = " UNION ALL ".join(evidence_queries)
        cr.execute(
            "UPDATE eh_investment_property prop SET "
            "source_provenance_quarantined = TRUE, "
            "source_provenance_quarantine_note = %s "
            "WHERE EXISTS ("
            " SELECT 1 FROM eh_investment_property_transfer log "
            " JOIN (" + union_sql + ") evidence "
            " ON evidence.asset_id = log.source_asset_id "
            " WHERE log.property_id = prop.id AND log.direction = 'in' "
            " AND evidence.evidence_date > log.date"
            ")",
            (
                "Fixed-asset accounting evidence exists after the recorded "
                "inbound transfer date. Historical entries were preserved; "
                "the conflicting owner chronology requires correction.",
            ),
        )
