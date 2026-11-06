# -*- encoding: utf-8 -*-
"""Preserve explicit openings and fail closed on impossible legacy values."""


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        "ALTER TABLE eh_nrv_line ADD COLUMN IF NOT EXISTS "
        "manual_opening_writedown numeric"
    )
    cr.execute(
        "UPDATE eh_nrv_line "
        "SET manual_opening_writedown = opening_writedown "
        "WHERE opening_writedown_manual "
        "AND manual_opening_writedown IS NULL"
    )
    cr.execute(
        """
        SELECT array_agg(id ORDER BY id)
          FROM eh_nrv_line
         WHERE cost < 0
            OR net_realisable_value < 0
            OR COALESCE(opening_writedown, 0) < 0
            OR COALESCE(opening_writedown, 0) > cost
            OR COALESCE(required_writedown, 0) < 0
            OR COALESCE(required_writedown, 0) > cost
        """
    )
    invalid_ids = cr.fetchone()[0]
    if invalid_ids:
        raise RuntimeError(
            "Inventory NRV upgrade blocked: legacy line(s) %s contain "
            "negative or above-cost measurement evidence. Correct and "
            "document those records before retrying; the migration will not "
            "silently rewrite posted accounting evidence."
            % ', '.join(str(line_id) for line_id in invalid_ids)
        )
