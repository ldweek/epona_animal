# -*- encoding: utf-8 -*-
"""Capture forged stored child anchors before related fields recompute."""

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
        "ALTER TABLE account_move ADD COLUMN IF NOT EXISTS "
        "eh_sbp_provenance_version integer DEFAULT 0"
    )
    cr.execute(
        """
        UPDATE eh_sbp_plan AS plan
           SET legacy_provenance_quarantined = TRUE,
               legacy_provenance_quarantine_note =
                   '19.0.1.0.4 upgrade audit: a stored child company or '
                   'currency anchor did not match its source plan before '
                   'related-field recomputation. Historical rows were '
                   'preserved and accounting is blocked pending review.'
          FROM res_company AS company
         WHERE company.id = plan.company_id
           AND (
                plan.currency_id IS DISTINCT FROM company.currency_id
                OR EXISTS (
                    SELECT 1 FROM eh_sbp_plan_tranche AS child
                     WHERE child.plan_id = plan.id
                       AND child.company_id IS DISTINCT FROM plan.company_id
                )
                OR EXISTS (
                    SELECT 1 FROM eh_sbp_grant AS child
                     WHERE child.plan_id = plan.id
                       AND (child.company_id IS DISTINCT FROM plan.company_id
                            OR child.currency_id IS DISTINCT FROM
                               company.currency_id)
                )
                OR EXISTS (
                    SELECT 1 FROM eh_sbp_modification AS child
                     WHERE child.plan_id = plan.id
                       AND (child.company_id IS DISTINCT FROM plan.company_id
                            OR child.currency_id IS DISTINCT FROM
                               company.currency_id)
                )
                OR EXISTS (
                    SELECT 1 FROM eh_sbp_period_run AS child
                     WHERE child.plan_id = plan.id
                       AND (child.company_id IS DISTINCT FROM plan.company_id
                            OR child.currency_id IS DISTINCT FROM
                               company.currency_id)
                )
           )
        """
    )
    if cr.rowcount:
        _logger.warning(
            "SBP 19.0.1.0.4 quarantined %d plan(s) with forged stored child "
            "company/currency anchors before recomputation.",
            cr.rowcount,
        )
