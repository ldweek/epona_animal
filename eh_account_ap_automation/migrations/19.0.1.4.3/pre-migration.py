# -*- encoding: utf-8 -*-
"""Fail closed before installing the one-default-per-company index."""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    # Choosing one of two concurrently-created approval policies would invent
    # control intent. Disable every default in an ambiguous company instead;
    # matching then stops until a manager deliberately selects one policy.
    cr.execute(
        """
        WITH ambiguous AS (
            SELECT company_id
              FROM eh_ap_tolerance_profile
             WHERE is_default IS TRUE
             GROUP BY company_id
            HAVING COUNT(*) > 1
        )
        UPDATE eh_ap_tolerance_profile AS profile
           SET is_default = FALSE,
               write_date = NOW()
          FROM ambiguous
         WHERE profile.company_id = ambiguous.company_id
           AND profile.is_default IS TRUE
        """,
    )
    if cr.rowcount:
        _logger.warning(
            "AP automation 19.0.1.4.3 disabled %d ambiguous default "
            "tolerance profiles; affected companies require manager review.",
            cr.rowcount,
        )
