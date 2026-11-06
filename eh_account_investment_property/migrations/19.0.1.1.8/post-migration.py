# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Align legacy IAS 40 subledgers with their immutable journal evidence."""


def migrate(cr, version):
    del version

    # The immutable inbound log is the authoritative recognition date. The
    # former default could be unrelated to the actual change in use.
    cr.execute(
        "UPDATE eh_investment_property prop SET acquisition_date = exact.date "
        "FROM ("
        " SELECT property_id, MIN(date) AS date "
        " FROM eh_investment_property_transfer WHERE direction = 'in' "
        " GROUP BY property_id"
        ") exact WHERE exact.property_id = prop.id "
        "AND prop.acquisition_date IS DISTINCT FROM exact.date"
    )

    # These terminal entries already cleared the gross and contra GL balances.
    # Clear the corresponding subledger totals without rewriting journal rows.
    cr.execute(
        "UPDATE eh_investment_property SET carrying_amount = 0, "
        "accumulated_depreciation = 0 "
        "WHERE state IN ('transferred', 'disposed') "
        "AND (COALESCE(carrying_amount, 0) != 0 "
        "OR COALESCE(accumulated_depreciation, 0) != 0)"
    )

    # Populate new period coverage/residual snapshots conservatively. Old
    # events represented a full fiscal-period charge by design.
    cr.execute(
        "UPDATE eh_investment_property_depreciation event SET "
        "coverage_start = GREATEST(event.period_start, prop.acquisition_date), "
        "coverage_end = event.period_end, "
        "residual_value_snapshot = COALESCE(prop.residual_value, 0) "
        "FROM eh_investment_property prop "
        "WHERE prop.id = event.property_id "
        "AND (event.coverage_start IS NULL OR event.coverage_end IS NULL)"
    )

    # Move an attributable reserve off the terminal source asset only where
    # the equity-account identity is exact (or one side is still unconfigured).
    # Conflicts remain visible and are quarantined instead of guessed.
    cr.execute(
        "UPDATE eh_investment_property prop SET "
        "source_provenance_quarantined = TRUE, "
        "source_provenance_quarantine_note = %s "
        "FROM eh_investment_property_transfer log "
        "JOIN eh_asset asset ON asset.id = log.source_asset_id "
        "WHERE log.property_id = prop.id AND log.direction = 'in' "
        "AND COALESCE(asset.revaluation_surplus, 0) > 0 "
        "AND (asset.revaluation_reserve_account_id IS NULL "
        "AND prop.revaluation_surplus_account_id IS NULL "
        "OR asset.revaluation_reserve_account_id IS NOT NULL "
        "AND prop.revaluation_surplus_account_id IS NOT NULL "
        "AND asset.revaluation_reserve_account_id "
        "!= prop.revaluation_surplus_account_id)",
        (
            "Legacy inbound revaluation surplus has no exact common equity "
            "account. The balance was preserved on the source asset and "
            "further property accounting is blocked pending correction.",
        ),
    )
    cr.execute(
        "UPDATE eh_investment_property prop SET "
        "revaluation_surplus = asset.revaluation_surplus, "
        "revaluation_surplus_account_id = COALESCE("
        " prop.revaluation_surplus_account_id, "
        " asset.revaluation_reserve_account_id) "
        "FROM eh_investment_property_transfer log "
        "JOIN eh_asset asset ON asset.id = log.source_asset_id "
        "WHERE log.property_id = prop.id AND log.direction = 'in' "
        "AND COALESCE(asset.revaluation_surplus, 0) > 0 "
        "AND prop.source_provenance_quarantined IS NOT TRUE "
        "AND COALESCE(prop.revaluation_surplus, 0) = 0"
    )
    cr.execute(
        "UPDATE eh_asset asset SET revaluation_surplus = 0 "
        "FROM eh_investment_property_transfer log "
        "JOIN eh_investment_property prop ON prop.id = log.property_id "
        "WHERE log.source_asset_id = asset.id AND log.direction = 'in' "
        "AND prop.source_provenance_quarantined IS NOT TRUE "
        "AND COALESCE(prop.revaluation_surplus, 0) > 0 "
        "AND prop.revaluation_surplus = asset.revaluation_surplus"
    )
