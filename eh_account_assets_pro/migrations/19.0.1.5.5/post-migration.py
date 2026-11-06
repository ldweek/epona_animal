# -*- coding: utf-8 -*-
"""Close parallel-book GL posting without rewriting historical evidence."""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # A flag on a book that never produced ledger evidence is only dormant
    # configuration, so it can be disabled safely.  Books with source or
    # reversal moves retain True as a legacy audit marker; runtime blocks new
    # postings while keeping the historical links and reversal action intact.
    cr.execute(
        """
        UPDATE eh_asset_book AS book
           SET posts_to_gl = FALSE,
               write_date = NOW()
         WHERE book.posts_to_gl IS TRUE
           AND NOT EXISTS (
                SELECT 1
                  FROM eh_asset_book_line AS line
                 WHERE line.book_id = book.id
                   AND (
                        line.is_posted IS TRUE
                        OR line.move_id IS NOT NULL
                        OR line.reversal_move_id IS NOT NULL
                   )
           )
        """,
    )
    disabled = cr.rowcount
    _logger.warning(
        "Assets Pro 19.0.1.5.5 disabled GL posting on %d unbooked "
        "parallel depreciation books; historical booked flags and journal "
        "links were retained as audit evidence.",
        disabled,
    )
