# -*- encoding: utf-8 -*-
"""Revalidate FX provenance for databases already upgraded to 1.6.3."""

from odoo.addons.eh_account_fx_revaluation.migration_utils import (
    migrate_fx_provenance,
)


def migrate(cr, version):
    del version
    migrate_fx_provenance(cr)
