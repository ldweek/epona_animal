# -*- encoding: utf-8 -*-
"""Backfill only explicit historic manual-letter evidence."""


def migrate(cr, version):
    del version
    cr.execute(
        """
        UPDATE eh_collections_action
           SET eh_ai_is_demand_letter = TRUE
         WHERE action_type = 'letter'
           AND NOT COALESCE(eh_ai_is_demand_letter, FALSE)
        """
    )
