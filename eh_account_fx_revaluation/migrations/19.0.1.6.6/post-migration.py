# -*- encoding: utf-8 -*-
"""Quarantine unproven legacy provider rates without guessing direction.

Releases through 1.6.5 wrote provider quotes reciprocally.  Those standard
``res.currency.rate`` rows carry no module/source marker, so migration cannot
distinguish them from operator, localization, or another connector's rows.
Blind inversion or deletion would corrupt legitimate data.  Disable every
existing config and hold FX revaluation until finance reviews the affected
company's rate history explicitly.
"""

from odoo.addons.eh_account_fx_revaluation.migration_utils import (
    migrate_fx_provenance,
)


_REVIEW_MESSAGE = (
    "Upgrade safety hold: review historical currency-rate rows written by "
    "FX providers before re-enabling updates or FX revaluation. Existing "
    "rows were not changed because source provenance is unavailable."
)


def migrate(cr, version):
    if not version:
        return
    migrate_fx_provenance(cr)
    cr.execute(
        """
        UPDATE eh_fx_rate_config
           SET enabled = FALSE,
               rate_direction_review_required = TRUE,
               rate_direction_reviewed_at = NULL,
               rate_direction_reviewed_by_id = NULL,
               last_fetched_at = NULL,
               last_error = %s,
               last_fetch_summary = %s
        """,
        [_REVIEW_MESSAGE, _REVIEW_MESSAGE],
    )
