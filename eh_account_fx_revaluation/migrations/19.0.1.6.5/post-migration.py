# -*- encoding: utf-8 -*-
"""Revalidate provenance and quarantine missing source-version snapshots."""

from odoo.addons.eh_account_fx_revaluation.migration_utils import (
    migrate_fx_provenance,
)


def migrate(cr, version):
    if not version:
        return
    migrate_fx_provenance(cr)
    cr.execute(
        """
        UPDATE eh_fx_revaluation_run
           SET source_move_version = -1
         WHERE state = 'computed'
           AND COALESCE(source_move_version, 0) = 0
        """
    )
