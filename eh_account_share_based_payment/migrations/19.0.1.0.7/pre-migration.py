# -*- encoding: utf-8 -*-
"""Fail closed for intermediate approved numeric/provenance evidence.

An intermediate 1.0.6 database could approve a manual or graded plan before
the linked-valuation equality gate landed. PostgreSQL also permits NaN and
Infinity in floating columns, and ordinary CHECK comparisons do not reject
them. Start from every non-draft plan (not only linked valuations), preserve
all rows, and quarantine any non-finite source/output or unproved linked
valuation graph.
"""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    note = (
        '19.0.1.0.7 measurement-evidence audit: an approved plan contained '
        'non-finite accounting inputs/outputs or linked valuation evidence '
        'that was cross-company, stale, non-positive, or different from its '
        'grant-date fair value at company-currency precision. Historical '
        'rows were preserved and future accounting is blocked pending '
        'controlled review.'
    )
    cr.execute(
        """
        WITH bad_plan AS (
            SELECT plan.id
              FROM eh_sbp_plan plan
         LEFT JOIN res_company company ON company.id = plan.company_id
         LEFT JOIN res_currency currency ON currency.id = plan.currency_id
             WHERE plan.state <> 'draft'
               AND (
                    company.id IS NULL
                    OR plan.currency_id IS DISTINCT FROM company.currency_id
                    OR currency.id IS NULL
                    OR COALESCE(currency.rounding::text, '') IN (
                        'NaN', 'Infinity', '-Infinity'
                    )
                    OR COALESCE(currency.rounding, 0) <= 0
                    OR COALESCE(plan.recognised_cumulative::text, '') IN (
                        'NaN', 'Infinity', '-Infinity'
                    )
                    OR COALESCE(plan.settlement_amount::text, '') IN (
                        'NaN', 'Infinity', '-Infinity'
                    )
                    OR EXISTS (
                        SELECT 1
                          FROM eh_sbp_grant grant_row
                         WHERE grant_row.plan_id = plan.id
                           AND (
                                grant_row.company_id IS DISTINCT FROM
                                    plan.company_id
                                OR grant_row.currency_id IS DISTINCT FROM
                                    plan.currency_id
                                OR COALESCE(
                                    grant_row.grant_date_fair_value::text, ''
                                ) IN ('NaN', 'Infinity', '-Infinity')
                                OR COALESCE(
                                    grant_row.exercise_price::text, ''
                                ) IN ('NaN', 'Infinity', '-Infinity')
                                OR COALESCE(
                                    grant_row.expected_forfeiture_pct::text,
                                    ''
                                ) IN ('NaN', 'Infinity', '-Infinity')
                           )
                    )
                    OR EXISTS (
                        SELECT 1
                          FROM eh_sbp_plan_tranche tranche
                         WHERE tranche.plan_id = plan.id
                           AND (
                                tranche.company_id IS DISTINCT FROM
                                    plan.company_id
                                OR COALESCE(tranche.portion_pct::text, '') IN (
                                    'NaN', 'Infinity', '-Infinity'
                                )
                                OR COALESCE(tranche.fair_value::text, '') IN (
                                    'NaN', 'Infinity', '-Infinity'
                                )
                           )
                    )
                    OR EXISTS (
                        SELECT 1
                          FROM eh_sbp_modification modification
                         WHERE modification.plan_id = plan.id
                           AND (
                                modification.company_id IS DISTINCT FROM
                                    plan.company_id
                                OR modification.currency_id IS DISTINCT FROM
                                    plan.currency_id
                                OR COALESCE(
                                    modification.incremental_fv::text, ''
                                ) IN ('NaN', 'Infinity', '-Infinity')
                           )
                    )
                    OR EXISTS (
                        SELECT 1
                          FROM eh_sbp_period_run run
                         WHERE run.plan_id = plan.id
                           AND (
                                run.company_id IS DISTINCT FROM plan.company_id
                                OR run.currency_id IS DISTINCT FROM
                                    plan.currency_id
                                OR COALESCE(
                                    run.current_fair_value::text, ''
                                ) IN ('NaN', 'Infinity', '-Infinity')
                                OR COALESCE(run.vested_fraction::text, '') IN (
                                    'NaN', 'Infinity', '-Infinity'
                                )
                                OR COALESCE(
                                    run.cumulative_target::text, ''
                                ) IN ('NaN', 'Infinity', '-Infinity')
                                OR COALESCE(
                                    run.prior_recognised::text, ''
                                ) IN ('NaN', 'Infinity', '-Infinity')
                                OR COALESCE(run.period_charge::text, '') IN (
                                    'NaN', 'Infinity', '-Infinity'
                                )
                           )
                    )
                    OR EXISTS (
                        SELECT 1
                          FROM eh_sbp_grant grant_row
                          JOIN eh_sbp_valuation valuation
                            ON valuation.id = grant_row.valuation_id
                         WHERE grant_row.plan_id = plan.id
                           AND (
                                valuation.company_id IS DISTINCT FROM
                                    plan.company_id
                                OR valuation.result_provenance_version
                                    IS DISTINCT FROM 1
                                OR COALESCE(valuation.spot::text, '') IN (
                                    'NaN', 'Infinity', '-Infinity'
                                )
                                OR COALESCE(valuation.strike::text, '') IN (
                                    'NaN', 'Infinity', '-Infinity'
                                )
                                OR COALESCE(
                                    valuation.volatility_pct::text, ''
                                ) IN ('NaN', 'Infinity', '-Infinity')
                                OR COALESCE(valuation.rate_pct::text, '') IN (
                                    'NaN', 'Infinity', '-Infinity'
                                )
                                OR COALESCE(valuation.term_years::text, '') IN (
                                    'NaN', 'Infinity', '-Infinity'
                                )
                                OR COALESCE(
                                    valuation.dividend_yield_pct::text, ''
                                ) IN ('NaN', 'Infinity', '-Infinity')
                                OR COALESCE(
                                    valuation.result_value::text, ''
                                ) IN ('NaN', 'Infinity', '-Infinity')
                                OR CASE
                                    WHEN COALESCE(
                                        grant_row.grant_date_fair_value::text,
                                        ''
                                    ) IN ('NaN', 'Infinity', '-Infinity')
                                    OR COALESCE(
                                        valuation.result_value::text, ''
                                    ) IN ('NaN', 'Infinity', '-Infinity')
                                    OR COALESCE(currency.rounding::text, '') IN (
                                        'NaN', 'Infinity', '-Infinity'
                                    )
                                    THEN TRUE
                                    ELSE ROUND(
                                        COALESCE(
                                            valuation.result_value, 0
                                        )::numeric
                                        / NULLIF(
                                            currency.rounding, 0
                                        )::numeric
                                    ) <= 0
                                    OR ROUND(
                                        COALESCE(
                                            grant_row.grant_date_fair_value, 0
                                        )::numeric
                                        / NULLIF(
                                            currency.rounding, 0
                                        )::numeric
                                    ) IS DISTINCT FROM ROUND(
                                        COALESCE(
                                            valuation.result_value, 0
                                        )::numeric
                                        / NULLIF(
                                            currency.rounding, 0
                                        )::numeric
                                    )
                                END
                           )
                    )
               )
        )
        UPDATE eh_sbp_plan plan
           SET legacy_provenance_quarantined = TRUE,
               legacy_provenance_quarantine_note = CASE
                   WHEN COALESCE(plan.legacy_provenance_quarantine_note, '') = ''
                   THEN %s
                   ELSE plan.legacy_provenance_quarantine_note || E'\n' || %s
               END
         WHERE plan.id IN (SELECT id FROM bad_plan)
           AND (
                plan.legacy_provenance_quarantined IS DISTINCT FROM TRUE
                OR POSITION(%s IN COALESCE(
                    plan.legacy_provenance_quarantine_note, ''
                )) = 0
           )
        """,
        (note, note, note),
    )
    if cr.rowcount:
        _logger.warning(
            "SBP 19.0.1.0.7 quarantined %d approved plan(s) with "
            "non-finite or unproved measurement evidence; all rows were "
            "preserved.",
            cr.rowcount,
        )
