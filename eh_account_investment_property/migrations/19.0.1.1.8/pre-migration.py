# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Quarantine dangling legacy integer source ids before adding the FK."""


def migrate(cr, version):
    del version
    cr.execute(
        "UPDATE eh_investment_property prop SET "
        "transfer_in_asset_display = COALESCE("
        " prop.transfer_in_asset_display, "
        " 'Unresolved legacy fixed asset id ' || prop.transfer_in_asset_id), "
        "source_provenance_quarantined = TRUE, "
        "source_provenance_quarantine_note = %s, "
        "transfer_in_asset_id = NULL "
        "WHERE prop.transfer_in_asset_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM eh_asset asset "
        " WHERE asset.id = prop.transfer_in_asset_id)",
        (
            "The former raw source id does not resolve to a current fixed "
            "asset. Its value was retained in the display snapshot; further "
            "accounting is blocked pending an explicit correction.",
        ),
    )
