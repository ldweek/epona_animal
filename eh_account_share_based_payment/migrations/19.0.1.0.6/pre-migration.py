# -*- encoding: utf-8 -*-
"""Fail closed for pre-control activation terms and valuation results.

Before 1.0.6, activation did not freeze the plan/grant/tranche measurement
basis and valuation results had no durable server provenance.  Neither a
plausible active plan nor a self-consistent stored option value proves what a
manager originally approved, so this migration does not shape-bless either.
"""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        "ALTER TABLE eh_sbp_plan ADD COLUMN IF NOT EXISTS "
        "legacy_provenance_quarantined boolean DEFAULT false"
    )
    cr.execute(
        "ALTER TABLE eh_sbp_plan ADD COLUMN IF NOT EXISTS "
        "legacy_provenance_quarantine_note text"
    )
    cr.execute(
        "ALTER TABLE eh_sbp_valuation ADD COLUMN IF NOT EXISTS "
        "result_provenance_version integer DEFAULT 0 NOT NULL"
    )

    # The column may already exist if an intermediate 1.0.6 build ran.  Reset
    # every pre-migration stamp as well as every nonzero legacy result: only an
    # action_compute call after this migration may create a trusted stamp.
    cr.execute(
        """
        UPDATE eh_sbp_valuation
           SET result_provenance_version = 0
         WHERE result_provenance_version IS DISTINCT FROM 0
            OR abs(COALESCE(result_value, 0)) > 0
        """
    )
    valuation_count = cr.rowcount

    note = (
        '19.0.1.0.6 activation audit: this plan had already left draft before '
        'award terms, posting accounts, and grant/tranche scope became '
        'immutable at activation. Historical rows were preserved and future '
        'accounting is blocked pending controlled review.'
    )
    cr.execute(
        """
        UPDATE eh_sbp_plan
           SET legacy_provenance_quarantined = TRUE,
               legacy_provenance_quarantine_note = CASE
                   WHEN COALESCE(legacy_provenance_quarantine_note, '') = ''
                   THEN %s
                   ELSE legacy_provenance_quarantine_note || E'\n' || %s
               END
         WHERE state <> 'draft'
        """,
        (note, note),
    )
    plan_count = cr.rowcount
    if valuation_count or plan_count:
        _logger.warning(
            "SBP 19.0.1.0.6 quarantined %d legacy valuation result(s) and "
            "%d pre-control non-draft plan(s); no stored shape or intermediate "
            "stamp was trusted.",
            valuation_count,
            plan_count,
        )
