# -*- encoding: utf-8 -*-
"""Backfill only FX journal provenance that passes structural validation."""

from odoo.addons.eh_account_fx_revaluation.migration_utils import (
    migrate_fx_provenance,
)


def migrate(cr, version):
    del version
    migrate_fx_provenance(cr)
