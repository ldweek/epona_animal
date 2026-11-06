# -*- encoding: utf-8 -*-
"""Backfill the explicit disposal-date input from posted legacy evidence."""

import logging


_logger = logging.getLogger(__name__)


def _backfill_disposal_dates(cr):
    """Copy only the existing result date; never infer or rewrite evidence."""
    cr.execute(
        """
        UPDATE eh_fair_value_item
           SET disposal_date = derecognition_date
         WHERE state = 'derecognised'
           AND disposal_date IS NULL
           AND derecognition_date IS NOT NULL
        """,
    )
    return cr.rowcount


def migrate(cr, version):
    if not version:
        return
    backfilled = _backfill_disposal_dates(cr)
    _logger.info(
        "Fair Value 19.0.1.1.6 backfilled %d explicit disposal date(s) "
        "from their immutable legacy derecognition-date evidence.",
        backfilled,
    )
