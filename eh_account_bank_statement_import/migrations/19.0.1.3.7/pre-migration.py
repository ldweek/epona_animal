# -*- coding: utf-8 -*-
"""Replace blanket journal uniqueness with active-profile uniqueness."""


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        "ALTER TABLE eh_bank_live_connector_profile "
        "DROP CONSTRAINT IF EXISTS "
        "eh_bank_live_connector_profile_unique_journal"
    )
