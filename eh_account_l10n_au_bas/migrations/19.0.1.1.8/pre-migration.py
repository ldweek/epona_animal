# -*- coding: utf-8 -*-
"""Replace one-return-per-period constraint with amendment sequence."""


def migrate(cr, version):
    del version
    cr.execute(
        "ALTER TABLE IF EXISTS eh_bas_run "
        "DROP CONSTRAINT IF EXISTS eh_bas_run_unique_period"
    )
