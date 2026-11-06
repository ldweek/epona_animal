# -*- encoding: utf-8 -*-
"""Repair 1.0.8 quarantine without fabricating lost provenance.

Some databases already ran the original 1.0.8 migration, which zeroed every
valuation result. Preserve any result still available, explicitly record when
a consumed result was already lost, and freeze old SBP moves with the common
legacy-unverified seal. Runtime manager review may then attest currency and
the cumulative anchor without promoting old evidence to verified status.
"""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    plan_columns = (
        ('legacy_provenance_review_currency_id', 'integer'),
        ('legacy_provenance_review_anchor', 'double precision'),
        ('legacy_provenance_review_anchor_confirmed',
         'boolean DEFAULT false'),
        ('legacy_provenance_review_note', 'text'),
        ('legacy_provenance_reviewed_at', 'timestamp'),
        ('legacy_provenance_reviewed_by_id', 'integer'),
        ('legacy_provenance_review_snapshot', 'text'),
        ('legacy_provenance_review_digest', 'varchar'),
        ('legacy_provenance_review_move_count', 'integer DEFAULT 0'),
        ('legacy_provenance_review_unverified_move_count',
         'integer DEFAULT 0'),
    )
    for name, definition in plan_columns:
        cr.execute(
            "ALTER TABLE eh_sbp_plan ADD COLUMN IF NOT EXISTS %s %s"
            % (name, definition)
        )

    valuation_columns = (
        ('legacy_result_value', 'double precision'),
        ('legacy_result_provenance_version', 'integer DEFAULT 0'),
        ('legacy_result_currency_id', 'integer'),
        ('legacy_result_unverified', 'boolean DEFAULT false'),
        ('legacy_result_quarantine_note', 'text'),
    )
    for name, definition in valuation_columns:
        cr.execute(
            "ALTER TABLE eh_sbp_valuation ADD COLUMN IF NOT EXISTS %s %s"
            % (name, definition)
        )
    cr.execute(
        "ALTER TABLE account_move ADD COLUMN IF NOT EXISTS "
        "eh_legacy_unverified_seal boolean DEFAULT false"
    )
    cr.execute(
        "ALTER TABLE account_move ADD COLUMN IF NOT EXISTS "
        "eh_sealed boolean DEFAULT false"
    )

    cr.execute(
        """
        UPDATE eh_sbp_valuation
           SET legacy_result_value = result_value,
               legacy_result_provenance_version =
                   result_provenance_version,
               legacy_result_currency_id = result_currency_id,
               legacy_result_unverified = TRUE,
               legacy_result_quarantine_note =
                   '19.0.1.0.9 preserved a remaining pre-anchor valuation '
                   'result as immutable unverified evidence.'
         WHERE legacy_result_unverified IS DISTINCT FROM TRUE
           AND (
                result_value IS DISTINCT FROM 0
                OR result_provenance_version IS DISTINCT FROM 0
                OR result_currency_id IS NOT NULL
           )
        """
    )
    preserved = cr.rowcount

    # A consumed result already zeroed by the old 1.0.8 script cannot be
    # reconstructed safely. The copied grant-date fair value remains frozen
    # source evidence; record loss explicitly rather than shape-blessing it.
    cr.execute(
        """
        UPDATE eh_sbp_valuation AS valuation
           SET legacy_result_unverified = TRUE,
               legacy_result_quarantine_note =
                   'Original 19.0.1.0.8 invalidation removed this consumed '
                   'valuation result before an evidence backup existed. '
                   'Linked approved grant fair values remain preserved; no '
                   'result was reconstructed or blessed.'
         WHERE valuation.legacy_result_unverified IS DISTINCT FROM TRUE
           AND EXISTS (
                   SELECT 1
                     FROM eh_sbp_grant AS award
                     JOIN eh_sbp_plan AS plan ON plan.id = award.plan_id
                    WHERE award.valuation_id = valuation.id
                      AND plan.state <> 'draft'
               )
        """
    )
    previously_lost = cr.rowcount

    cr.execute(
        """
        UPDATE eh_sbp_valuation AS valuation
           SET result_provenance_version = 0,
               result_currency_id = NULL
         WHERE valuation.legacy_result_unverified IS TRUE
           AND EXISTS (
                   SELECT 1
                     FROM eh_sbp_grant AS award
                     JOIN eh_sbp_plan AS plan ON plan.id = award.plan_id
                    WHERE award.valuation_id = valuation.id
                      AND plan.state <> 'draft'
               )
        """
    )
    cr.execute(
        """
        UPDATE eh_sbp_valuation AS valuation
           SET result_value = 0,
               result_provenance_version = 0,
               result_currency_id = NULL
         WHERE valuation.legacy_result_unverified IS TRUE
           AND NOT EXISTS (
                   SELECT 1
                     FROM eh_sbp_grant AS award
                     JOIN eh_sbp_plan AS plan ON plan.id = award.plan_id
                    WHERE award.valuation_id = valuation.id
                      AND plan.state <> 'draft'
               )
        """
    )
    invalidated = cr.rowcount

    cr.execute(
        """
        UPDATE account_move AS move
           SET eh_legacy_unverified_seal = TRUE,
               eh_sealed = FALSE
          FROM eh_sbp_plan AS plan
         WHERE move.eh_sbp_plan_id = plan.id
           AND plan.legacy_provenance_quarantined IS TRUE
           AND move.state = 'posted'
           AND (
                move.eh_legacy_unverified_seal IS DISTINCT FROM TRUE
                OR move.eh_sealed IS DISTINCT FROM FALSE
           )
        """
    )
    frozen_moves = cr.rowcount
    if preserved or previously_lost or invalidated or frozen_moves:
        _logger.warning(
            "SBP 19.0.1.0.9 preserved %d remaining valuation result(s), "
            "recorded %d already-lost consumed result(s), invalidated %d "
            "unconsumed result(s), and froze %d legacy move(s) without "
            "promoting provenance.",
            preserved,
            previously_lost,
            invalidated,
            frozen_moves,
        )
