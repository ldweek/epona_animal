# -*- encoding: utf-8 -*-
"""Capture forged stored company/revision anchors before recomputation."""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    # 1.0.0/1.0.1 schemas predate correction lineage. Pre-migrations run
    # before ORM field setup, so every reference below must first make its
    # nullable audit column available. ORM upgrade later adds the FK/index.
    cr.execute(
        "ALTER TABLE eh_benefit_valuation ADD COLUMN IF NOT EXISTS "
        "supersedes_id integer"
    )
    for table in ('eh_benefit_dc_accrual', 'eh_benefit_valuation'):
        cr.execute(
            "ALTER TABLE %s ADD COLUMN IF NOT EXISTS "
            "provenance_quarantined boolean DEFAULT false" % table
        )
        cr.execute(
            "ALTER TABLE %s ADD COLUMN IF NOT EXISTS "
            "provenance_quarantine_note text" % table
        )
    cr.execute(
        "ALTER TABLE account_move ADD COLUMN IF NOT EXISTS "
        "eh_benefit_provenance_version integer DEFAULT 0"
    )

    cr.execute(
        """
        UPDATE eh_benefit_dc_accrual AS accrual
           SET provenance_quarantined = TRUE,
               provenance_quarantine_note =
                   '19.0.1.0.3 upgrade audit: the stored company/currency '
                   'anchor did not reconcile before related-field '
                   'recomputation. Evidence was preserved and accounting is '
                   'blocked pending review.'
          FROM res_company AS company
         WHERE company.id = accrual.company_id
           AND accrual.currency_id IS DISTINCT FROM company.currency_id
        """
    )
    dc_count = cr.rowcount
    cr.execute(
        """
        UPDATE eh_benefit_valuation AS valuation
           SET provenance_quarantined = TRUE,
               provenance_quarantine_note =
                   '19.0.1.0.3 upgrade audit: the stored valuation company '
                   'or currency did not reconcile, or a pre-control '
                   'correction lineage already existed. Legacy correction '
                   'links are never trusted from shape. Evidence was '
                   'preserved and accounting is blocked pending review.'
          FROM eh_benefit_plan AS plan
          JOIN res_company AS company ON company.id = plan.company_id
         WHERE plan.id = valuation.plan_id
           AND (
                valuation.company_id IS DISTINCT FROM plan.company_id
                OR valuation.currency_id IS DISTINCT FROM company.currency_id
                OR EXISTS (
                    SELECT 1
                      FROM eh_benefit_valuation AS prior
                     WHERE prior.id = valuation.supersedes_id
                       AND (prior.plan_id IS DISTINCT FROM valuation.plan_id
                            OR prior.period_end IS DISTINCT FROM
                               valuation.period_end
                            OR prior.state != 'reversed')
                )
                OR EXISTS (
                    SELECT 1
                      FROM eh_benefit_valuation AS correction
                     WHERE correction.supersedes_id = valuation.id
                       AND (valuation.state != 'reversed'
                            OR correction.plan_id IS DISTINCT FROM
                               valuation.plan_id
                            OR correction.period_end IS DISTINCT FROM
                               valuation.period_end)
                )
           )
        """
    )
    valuation_count = cr.rowcount
    if dc_count or valuation_count:
        _logger.warning(
            "Employee Benefits 19.0.1.0.3 quarantined %d DC accrual(s) and "
            "%d valuation(s) with forged stored anchors before "
            "recomputation.",
            dc_count, valuation_count,
        )
