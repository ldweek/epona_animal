# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Preserve legacy discontinued-operation scope in immutable snapshots."""


def migrate(cr, version):
    del version
    # The selected accounts on each tagged single record were the explicit
    # workflow input. Preserve that record-local scope; do not infer it from
    # the shared, mutable account tag.
    cr.execute(
        "INSERT INTO eh_hfs_disc_evidence_account_rel (item_id, account_id) "
        "SELECT DISTINCT rel.item_id, rel.account_id "
        "FROM eh_held_for_sale_disc_account_rel rel "
        "JOIN eh_held_for_sale item ON item.id = rel.item_id "
        "WHERE item.discontinued_tag_id IS NOT NULL "
        "ON CONFLICT DO NOTHING"
    )

    # Disposal-group scope was explicitly selected per member line. Snapshot
    # the current record-local selection for legacy tagged groups; again, the
    # shared tag is presentation metadata and is not used to manufacture scope.
    cr.execute(
        "INSERT INTO eh_dg_disc_evidence_account_rel (group_id, account_id) "
        "SELECT DISTINCT line.group_id, line.pl_account_id "
        "FROM eh_disposal_group_line line "
        "JOIN eh_disposal_group grp ON grp.id = line.group_id "
        "WHERE grp.discontinued_tag_id IS NOT NULL "
        "AND line.pl_account_id IS NOT NULL "
        "ON CONFLICT DO NOTHING"
    )
