# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Repair the unsafe 1.1.0 legacy-origin assumption without losing evidence.

Databases that already ran the original 1.1.0 migration have pre-control rows
labelled ``extracted`` even though the old schema retained no origin evidence.
There is no safe discriminator between those rows and rows genuinely rebuilt
after that upgrade, so quarantine every extant extracted row. The earlier
migration also inferred an instrument/liability class without evidence. Keep
both pre-quarantine labels in explicit audit columns, mark the operational
class ambiguous, and leave amounts, bands, run links, authors and timestamps
untouched until a manager explicitly resolves the row.
"""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        UPDATE eh_fin_maturity_line
           SET legacy_claimed_origin = origin,
               legacy_claimed_item_class = item_class,
               origin = 'legacy_ambiguous',
               item_class = 'legacy_unknown'
         WHERE origin = 'extracted'
        """,
    )
    quarantined = cr.rowcount
    cr.execute(
        """
        UPDATE eh_fin_maturity_run r
           SET total_undiscounted = COALESCE((
               SELECT SUM(l.undiscounted_amount)
                 FROM eh_fin_maturity_line l
                WHERE l.run_id = r.id
                  AND l.origin IN ('manual', 'extracted')
           ), 0.0)
        """,
    )
    if quarantined:
        _logger.warning(
            "eh_account_disclosures 19.0.1.1.2: quarantined %d existing "
            "maturity rows with unprovable legacy origin/class; the old "
            "labels were retained as audit claims and amounts, bands, run "
            "links, users and timestamps were preserved.",
            quarantined,
        )
