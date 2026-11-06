# -*- coding: utf-8 -*-
"""Retire loose group aliases without rewriting historical reporting data."""

import logging


_logger = logging.getLogger(__name__)


def _column_exists(cr, table, column):
    cr.execute(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = %s
           AND column_name = %s
        """,
        (table, column),
    )
    return bool(cr.fetchone())


def migrate(cr, version):
    if not version:
        return

    # The former item.group_id was only a second UI pointer: it did not bind to
    # a member or receive the group's measurement. Preserve its human-readable
    # audit identity, then retire the operational pointer. Disposal-group lines
    # are now the sole group measurement records.
    if _column_exists(cr, 'eh_held_for_sale', 'group_id'):
        cr.execute(
            """
            UPDATE eh_held_for_sale AS item
               SET legacy_group_reference = COALESCE(
                       item.legacy_group_reference,
                       'Retired loose disposal-group reference: '
                       || group_record.name || ' [id:'
                       || group_record.id::text || ']'
                   ),
                   group_id = NULL,
                   write_date = NOW()
              FROM eh_disposal_group AS group_record
             WHERE item.group_id = group_record.id
            """,
        )
        _logger.info(
            "HFS 19.0.2.1.1 retired %d loose item/group aliases while "
            "preserving their display identity.",
            cr.rowcount,
        )
