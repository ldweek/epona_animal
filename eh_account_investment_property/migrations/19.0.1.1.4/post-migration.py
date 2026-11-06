# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Bind legacy inbound transfers to their source and terminalise it safely."""

from odoo.exceptions import UserError


def _assert_no_row(cr, query, message):
    cr.execute(query)
    row = cr.fetchone()
    if row:
        raise UserError("%s First conflicting id: %s." % (message, row[0]))


def migrate(cr, version):
    del version
    _assert_no_row(
        cr,
        "SELECT log.id "
        "FROM eh_investment_property_transfer log "
        "JOIN eh_investment_property prop ON prop.id = log.property_id "
        "LEFT JOIN eh_asset asset ON asset.id = prop.transfer_in_asset_id "
        "WHERE log.direction = 'in' AND log.source_asset_id IS NULL "
        "AND (asset.id IS NULL OR asset.company_id != prop.company_id) "
        "LIMIT 1",
        "Legacy inbound investment-property evidence has no unambiguous "
        "same-company source asset; the upgrade will not fabricate one.",
    )
    _assert_no_row(
        cr,
        "SELECT MIN(log.id) "
        "FROM eh_investment_property_transfer log "
        "JOIN eh_investment_property prop ON prop.id = log.property_id "
        "WHERE log.direction = 'in' "
        "GROUP BY prop.transfer_in_asset_id HAVING COUNT(*) > 1 LIMIT 1",
        "One fixed asset is linked to multiple inbound transfer events.",
    )
    _assert_no_row(
        cr,
        "SELECT log.id "
        "FROM eh_investment_property_transfer log "
        "JOIN eh_investment_property prop ON prop.id = log.property_id "
        "LEFT JOIN account_move move ON move.id = log.move_id "
        "WHERE log.direction = 'in' AND (move.id IS NULL "
        "OR move.state != 'posted' OR move.eh_sealed IS NOT TRUE "
        "OR move.eh_investment_property_id IS DISTINCT FROM prop.id) "
        "LIMIT 1",
        "Legacy inbound transfer has missing or conflicting sealed journal "
        "provenance.",
    )
    _assert_no_row(
        cr,
        "SELECT asset.id "
        "FROM eh_investment_property_transfer log "
        "JOIN eh_investment_property prop ON prop.id = log.property_id "
        "JOIN eh_asset asset ON asset.id = prop.transfer_in_asset_id "
        "WHERE log.direction = 'in' AND (asset.lvp_pool_id IS NOT NULL "
        "OR asset.disposal_move_id IS NOT NULL "
        "AND asset.disposal_move_id != log.move_id) LIMIT 1",
        "Legacy inbound source has pool or conflicting disposal evidence.",
    )
    _assert_no_row(
        cr,
        "SELECT line.id "
        "FROM eh_investment_property_transfer log "
        "JOIN eh_investment_property prop ON prop.id = log.property_id "
        "JOIN eh_asset_depreciation_line line "
        "ON line.asset_id = prop.transfer_in_asset_id "
        "WHERE log.direction = 'in' AND line.is_posted IS TRUE "
        "AND line.depreciation_date > log.date LIMIT 1",
        "A legacy source asset has depreciation posted after its inbound "
        "transfer; terminalising it would hide conflicting ledger evidence.",
    )

    cr.execute(
        "UPDATE eh_investment_property_transfer log "
        "SET source_asset_id = prop.transfer_in_asset_id "
        "FROM eh_investment_property prop "
        "WHERE prop.id = log.property_id AND log.direction = 'in' "
        "AND log.source_asset_id IS NULL"
    )
    cr.execute(
        "UPDATE eh_asset asset SET "
        "state = 'disposed', "
        "disposed_at = COALESCE(asset.disposed_at, log.create_date), "
        "disposed_by_id = COALESCE(asset.disposed_by_id, log.create_uid), "
        "disposal_date = log.date, disposal_proceeds = 0, "
        "disposal_partner_id = NULL, disposal_move_id = log.move_id "
        "FROM eh_investment_property_transfer log "
        "WHERE log.direction = 'in' "
        "AND log.source_asset_id = asset.id"
    )
