# -*- encoding: utf-8 -*-
"""Fail closed for measurement evidence predating currency snapshots.

Before 1.0.8, valuation results and approved plans recorded only a related
company currency. A company-currency change could therefore relabel historical
amounts without leaving evidence of their original denomination. No legacy
shape can prove that missing fact. Preserve every pre-upgrade result in
dedicated immutable evidence columns. Invalidate results only where no
approved plan consumed them and a current computation remains possible.
Consumed results remain numerically intact but unproved, and their plans are
quarantined for controlled manager review.
"""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        "ALTER TABLE eh_sbp_plan ADD COLUMN IF NOT EXISTS "
        "measurement_currency_id integer"
    )
    cr.execute(
        "ALTER TABLE eh_sbp_valuation ADD COLUMN IF NOT EXISTS "
        "result_currency_id integer"
    )
    cr.execute(
        "ALTER TABLE eh_sbp_valuation ADD COLUMN IF NOT EXISTS "
        "legacy_result_value double precision"
    )
    cr.execute(
        "ALTER TABLE eh_sbp_valuation ADD COLUMN IF NOT EXISTS "
        "legacy_result_provenance_version integer DEFAULT 0"
    )
    cr.execute(
        "ALTER TABLE eh_sbp_valuation ADD COLUMN IF NOT EXISTS "
        "legacy_result_currency_id integer"
    )
    cr.execute(
        "ALTER TABLE eh_sbp_valuation ADD COLUMN IF NOT EXISTS "
        "legacy_result_unverified boolean DEFAULT false"
    )
    cr.execute(
        "ALTER TABLE eh_sbp_valuation ADD COLUMN IF NOT EXISTS "
        "legacy_result_quarantine_note text"
    )
    note = (
        '19.0.1.0.8 currency-provenance audit: this approved plan predates '
        'the durable measurement-currency snapshot, so the denomination of '
        'its frozen terms cannot be proved. Historical rows were preserved '
        'and future accounting is blocked pending controlled review.'
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
           AND (
                legacy_provenance_quarantined IS DISTINCT FROM TRUE
                OR POSITION(%s IN COALESCE(
                    legacy_provenance_quarantine_note, ''
                )) = 0
           )
        """,
        (note, note, note),
    )
    quarantined = cr.rowcount
    cr.execute(
        """
        UPDATE eh_sbp_valuation
           SET legacy_result_value = result_value,
               legacy_result_provenance_version =
                   result_provenance_version,
               legacy_result_currency_id = result_currency_id,
               legacy_result_unverified = TRUE,
               legacy_result_quarantine_note =
                   '19.0.1.0.8 preserved this pre-currency-anchor option '
                   'valuation as unverified historical evidence. It cannot '
                   'be consumed by a new grant.'
         WHERE legacy_result_unverified IS DISTINCT FROM TRUE
           AND (
                result_value IS DISTINCT FROM 0
                OR result_provenance_version IS DISTINCT FROM 0
                OR result_currency_id IS NOT NULL
           )
        """,
    )
    preserved = cr.rowcount
    cr.execute(
        """
        UPDATE eh_sbp_valuation AS valuation
           SET result_provenance_version = 0,
               result_currency_id = NULL,
               legacy_result_quarantine_note =
                   COALESCE(valuation.legacy_result_quarantine_note, '') ||
                   ' Result remains numerically frozen because an approved '
                   'plan consumed it; recomputation would rewrite evidence.'
         WHERE EXISTS (
                   SELECT 1
                     FROM eh_sbp_grant AS award
                     JOIN eh_sbp_plan AS plan ON plan.id = award.plan_id
                    WHERE award.valuation_id = valuation.id
                      AND plan.state <> 'draft'
               )
           AND valuation.legacy_result_unverified IS TRUE
           AND POSITION(
                'Result remains numerically frozen' IN COALESCE(
                    valuation.legacy_result_quarantine_note, ''
                )
           ) = 0
        """,
    )
    consumed = cr.rowcount
    cr.execute(
        """
        UPDATE eh_sbp_valuation AS valuation
           SET result_value = 0,
               result_provenance_version = 0,
               result_currency_id = NULL,
               legacy_result_quarantine_note =
                   COALESCE(valuation.legacy_result_quarantine_note, '') ||
                   ' Current result was invalidated because no approved plan '
                   'consumed it; manager recomputation is required before '
                   'future use.'
         WHERE valuation.legacy_result_unverified IS TRUE
           AND NOT EXISTS (
                   SELECT 1
                     FROM eh_sbp_grant AS award
                     JOIN eh_sbp_plan AS plan ON plan.id = award.plan_id
                    WHERE award.valuation_id = valuation.id
                      AND plan.state <> 'draft'
               )
           AND (
                valuation.result_value IS DISTINCT FROM 0
                OR valuation.result_provenance_version IS DISTINCT FROM 0
                OR valuation.result_currency_id IS NOT NULL
           )
        """,
    )
    invalidated = cr.rowcount
    if quarantined or preserved or consumed or invalidated:
        _logger.warning(
            "SBP 19.0.1.0.8 quarantined %d pre-anchor approved plan(s) and "
            "preserved %d legacy valuation result(s); %d consumed result(s) "
            "were frozen unproved and %d unconsumed result(s) invalidated "
            "for current recomputation.",
            quarantined,
            preserved,
            consumed,
            invalidated,
        )
