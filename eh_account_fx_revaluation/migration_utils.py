# -*- encoding: utf-8 -*-
"""Fail-closed upgrade validation for legacy FX journal provenance."""

import math

from odoo.exceptions import UserError
from odoo.tools import float_is_zero, float_round


def _assert_no_rows(cr, query, message, params=()):
    cr.execute(query, params)
    row = cr.fetchone()
    if row:
        raise UserError("%s First conflicting id: %s." % (message, row[0]))


def _finite_amount(value, label, source_id):
    try:
        amount = float(value or 0.0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise UserError(
            "%s contains an invalid amount on source %s."
            % (label, source_id)
        ) from exc
    if not math.isfinite(amount):
        raise UserError(
            "%s contains a non-finite amount on source %s."
            % (label, source_id)
        )
    return amount


def _fetch_move_payloads(cr, move_ids):
    payloads = {int(move_id): [] for move_id in move_ids if move_id}
    if not payloads:
        return payloads
    cr.execute(
        "SELECT line.move_id, line.account_id, line.partner_id, line.debit, "
        "line.credit, line.balance, line.currency_id, line.amount_currency, "
        "company.currency_id FROM account_move_line line "
        "JOIN account_move move ON move.id = line.move_id "
        "JOIN res_company company ON company.id = move.company_id "
        "WHERE line.move_id IN %s ORDER BY line.id",
        (tuple(sorted(payloads)),),
    )
    for (
        move_id, account_id, partner_id, debit, credit, raw_balance,
        currency_id, raw_amount_currency, company_currency_id,
    ) in cr.fetchall():
        debit = _finite_amount(debit, "Journal entry", move_id)
        credit = _finite_amount(credit, "Journal entry", move_id)
        balance = _finite_amount(raw_balance, "Journal entry", move_id)
        amount_currency = _finite_amount(
            raw_amount_currency, "Journal entry", move_id,
        )
        if debit < 0.0 or credit < 0.0 or (debit > 0.0 and credit > 0.0):
            raise UserError(
                "Journal entry contains an invalid debit/credit row on "
                "move %s." % move_id
            )
        if abs(balance - (debit - credit)) >= 0.000001:
            raise UserError(
                "Journal entry contains an inconsistent stored balance on "
                "move %s." % move_id
            )
        if (
            currency_id != company_currency_id
            or abs(amount_currency - balance) >= 0.000001
        ):
            raise UserError(
                "Journal entry contains an inconsistent functional-currency "
                "projection on move %s." % move_id
            )
        payloads[move_id].append((
            account_id or False, partner_id or False, debit, credit,
        ))
    return payloads


def _assert_exact_payload(label, source_id, actual, expected):
    def key(row):
        return (row[0] or 0, row[1] or 0, row[2], row[3])

    actual = sorted(actual, key=key)
    expected = sorted(expected, key=key)
    if len(actual) != len(expected):
        raise UserError(
            "%s journal payload does not match source %s."
            % (label, source_id)
        )
    for actual_row, expected_row in zip(actual, expected):
        if (
            actual_row[:2] != expected_row[:2]
            or abs(actual_row[2] - expected_row[2]) >= 0.000001
            or abs(actual_row[3] - expected_row[3]) >= 0.000001
        ):
            raise UserError(
                "%s journal payload does not match source %s."
                % (label, source_id)
            )


def _signed_payload_row(account_id, partner_id, balance):
    return (
        account_id or False,
        partner_id or False,
        balance if balance > 0.0 else 0.0,
        -balance if balance < 0.0 else 0.0,
    )


def _backfill_link(cr, source_table, source_move_field, target_field, label,
                   company_join, company_field):
    _assert_no_rows(
        cr,
        "SELECT {field} FROM {table} WHERE {field} IS NOT NULL "
        "GROUP BY {field} HAVING COUNT(*) > 1 LIMIT 1".format(
            field=source_move_field, table=source_table),
        "%s move is linked to multiple source records; provenance is "
        "ambiguous and the upgrade cannot continue." % label,
    )
    _assert_no_rows(
        cr,
        "SELECT src.id FROM {table} src "
        "JOIN account_move move ON move.id = src.{move_field} "
        "{company_join} "
        "WHERE src.{move_field} IS NOT NULL "
        "AND (move.company_id != {company_field} "
        "OR move.state != 'posted' OR move.eh_sealed IS NOT TRUE "
        "OR move.move_type != 'entry') LIMIT 1".format(
            table=source_table,
            move_field=source_move_field,
            company_join=company_join,
            company_field=company_field,
        ),
        "%s source and posted sealed move do not agree." % label,
    )
    _assert_no_rows(
        cr,
        "SELECT src.id FROM {table} src "
        "JOIN account_move move ON move.id = src.{move_field} "
        "WHERE src.{move_field} IS NOT NULL "
        "AND move.{target_field} IS NOT NULL "
        "AND move.{target_field} != src.id LIMIT 1".format(
            table=source_table,
            move_field=source_move_field,
            target_field=target_field,
        ),
        "%s move already carries conflicting provenance." % label,
    )
    _assert_no_rows(
        cr,
        "SELECT move.id FROM account_move move "
        "LEFT JOIN {table} src ON src.id = move.{target_field} "
        "WHERE move.{target_field} IS NOT NULL "
        "AND (src.id IS NULL OR src.{move_field} IS DISTINCT FROM move.id) "
        "LIMIT 1".format(
            table=source_table,
            target_field=target_field,
            move_field=source_move_field,
        ),
        "%s target backlink does not match its source pointer." % label,
    )
    cr.execute(
        "UPDATE account_move move SET {target_field} = src.id "
        "FROM {table} src "
        "WHERE src.{move_field} = move.id "
        "AND move.{target_field} IS NULL".format(
            target_field=target_field,
            table=source_table,
            source_move_field=source_move_field,
            move_field=source_move_field,
        )
    )


def _validate_account_types(cr):
    _assert_no_rows(
        cr,
        """
        SELECT run.id
          FROM eh_fx_revaluation_run run
          JOIN account_account gain ON gain.id = run.gain_account_id
          JOIN account_account loss ON loss.id = run.loss_account_id
         WHERE gain.account_type != 'income_other'
            OR loss.account_type != 'expense'
         LIMIT 1
        """,
        "FX revaluation gain/loss accounts have incompatible account types.",
    )
    _assert_no_rows(
        cr,
        """
        SELECT hedge.id
          FROM eh_fx_hedge hedge
         WHERE EXISTS (
                   SELECT 1 FROM account_account account
                    WHERE account.id = hedge.oci_account_id
                      AND account.account_type NOT IN (
                          'equity', 'equity_unaffected'
                      )
               )
            OR EXISTS (
                   SELECT 1 FROM account_account account
                    WHERE account.id = hedge.pl_account_id
                      AND account.account_type NOT IN (
                          'income', 'income_other', 'expense', 'expense_other',
                          'expense_depreciation', 'expense_direct_cost'
                      )
               )
            OR EXISTS (
                   SELECT 1 FROM account_account account
                    WHERE account.id IN (
                        hedge.instrument_account_id,
                        hedge.hedged_item_account_id
                    )
                      AND account.account_type NOT IN (
                          'asset_receivable', 'asset_cash', 'asset_current',
                          'asset_non_current', 'asset_prepayments',
                          'asset_fixed', 'liability_payable',
                          'liability_credit_card', 'liability_current',
                          'liability_non_current'
                      )
               )
         LIMIT 1
        """,
        "FX hedge posting accounts have incompatible account types.",
    )
    _assert_no_rows(
        cr,
        """
        SELECT pos.id
          FROM eh_fx_cta_position pos
         WHERE EXISTS (
                   SELECT 1 FROM account_account account
                    WHERE account.id = pos.cta_account_id
                      AND account.account_type NOT IN (
                          'equity', 'equity_unaffected'
                      )
               )
            OR EXISTS (
                   SELECT 1 FROM account_account account
                    WHERE account.id = pos.gain_account_id
                      AND account.account_type != 'income_other'
               )
            OR EXISTS (
                   SELECT 1 FROM account_account account
                    WHERE account.id = pos.loss_account_id
                      AND account.account_type != 'expense'
               )
         LIMIT 1
        """,
        "CTA reserve/disposal accounts have incompatible account types.",
    )


def _validate_effectiveness_evidence(cr):
    _assert_no_rows(
        cr,
        """
        SELECT test.id
          FROM eh_fx_hedge_test test
          JOIN eh_fx_hedge hedge ON hedge.id = test.hedge_id
         WHERE hedge.state = 'draft'
            OR test.test_date < hedge.designation_date
         LIMIT 1
        """,
        "Pre-designation hedge-effectiveness evidence cannot be trusted; "
        "remove or remediate it before upgrading.",
    )

    cr.execute(
        "SELECT id, rsquared_threshold FROM eh_fx_hedge ORDER BY id"
    )
    thresholds = {}
    for hedge_id, raw_threshold in cr.fetchall():
        threshold = _finite_amount(
            raw_threshold or 0.80,
            "Hedge-effectiveness threshold", hedge_id,
        )
        if not 0.0 <= threshold <= 1.0:
            raise UserError(
                "Hedge %s has an R-squared threshold outside [0, 1]."
                % hedge_id
            )
        thresholds[hedge_id] = threshold

    cr.execute(
        """
        SELECT test.id, test.hedge_id, test.method,
               test.cumulative_instrument_change,
               test.cumulative_hedged_change, test.regression_pairs,
               test.offset_ratio, test.rsquared, test.slope,
               test.is_effective, hedge.rsquared_threshold
          FROM eh_fx_hedge_test test
          JOIN eh_fx_hedge hedge ON hedge.id = test.hedge_id
         ORDER BY test.id
        """
    )
    test_results = {}
    for (
        test_id, hedge_id, method, raw_instrument, raw_hedged,
        regression_pairs, raw_ratio, raw_rsquared, raw_slope,
        stored_effective, raw_threshold,
    ) in cr.fetchall():
        instrument = _finite_amount(
            raw_instrument, "Hedge-effectiveness test", test_id,
        )
        hedged = _finite_amount(
            raw_hedged, "Hedge-effectiveness test", test_id,
        )
        stored_ratio = _finite_amount(
            raw_ratio, "Hedge-effectiveness test result", test_id,
        )
        stored_rsquared = _finite_amount(
            raw_rsquared, "Hedge-effectiveness test result", test_id,
        )
        stored_slope = _finite_amount(
            raw_slope, "Hedge-effectiveness test result", test_id,
        )
        threshold = thresholds[hedge_id]
        if method == 'dollar_offset':
            denominator = abs(hedged)
            ratio = abs(instrument) / denominator if denominator else 0.0
            if not math.isfinite(ratio):
                raise UserError(
                    "Hedge-effectiveness test %s produces a non-finite "
                    "dollar-offset ratio." % test_id
                )
            expected_ratio = round(ratio, 4)
            expected_rsquared = 0.0
            expected_slope = 0.0
            expected_effective = bool(
                denominator and 0.80 <= ratio <= 1.25
            )
        elif method == 'regression':
            pairs = []
            for line in (regression_pairs or '').splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(',')
                if len(parts) != 2:
                    continue
                try:
                    x_value = float(parts[0].strip())
                    y_value = float(parts[1].strip())
                except (TypeError, ValueError, OverflowError):
                    continue
                if not math.isfinite(x_value) or not math.isfinite(y_value):
                    raise UserError(
                        "Hedge-effectiveness test %s contains non-finite "
                        "regression inputs." % test_id
                    )
                pairs.append((x_value, y_value))
            if len(pairs) < 2:
                rsquared = 0.0
                slope = 0.0
            else:
                scale_x = max(abs(x_value) for x_value, _ in pairs)
                scale_y = max(abs(y_value) for _, y_value in pairs)
                if not scale_x or not scale_y:
                    rsquared = 0.0
                    slope = 0.0
                else:
                    normalised = [
                        (x_value / scale_x, y_value / scale_y)
                        for x_value, y_value in pairs
                    ]
                    count = len(normalised)
                    mean_x = math.fsum(
                        x_value for x_value, _ in normalised
                    ) / count
                    mean_y = math.fsum(
                        y_value for _, y_value in normalised
                    ) / count
                    ss_xx = math.fsum(
                        (x_value - mean_x) ** 2
                        for x_value, _ in normalised
                    )
                    ss_yy = math.fsum(
                        (y_value - mean_y) ** 2
                        for _, y_value in normalised
                    )
                    ss_xy = math.fsum(
                        (x_value - mean_x) * (y_value - mean_y)
                        for x_value, y_value in normalised
                    )
                    rsquared = (
                        (ss_xy / ((ss_xx * ss_yy) ** 0.5)) ** 2
                        if ss_xx and ss_yy else 0.0
                    )
                    slope = (
                        (ss_xy / ss_xx) * (scale_y / scale_x)
                        if ss_xx else 0.0
                    )
                    if not math.isfinite(rsquared) or not math.isfinite(slope):
                        raise UserError(
                            "Hedge-effectiveness test %s produces "
                            "non-finite regression results." % test_id
                        )
            expected_ratio = 0.0
            expected_rsquared = round(rsquared, 4)
            expected_slope = round(slope, 4)
            expected_effective = bool(
                len(pairs) >= 2
                and rsquared >= threshold
                and slope < 0.0
                and 0.80 <= abs(slope) <= 1.25
            )
        else:
            raise UserError(
                "Hedge-effectiveness test %s has an unsupported method."
                % test_id
            )
        if (
            abs(stored_ratio - expected_ratio) >= 0.000001
            or abs(stored_rsquared - expected_rsquared) >= 0.000001
            or abs(stored_slope - expected_slope) >= 0.000001
            or bool(stored_effective) != expected_effective
        ):
            raise UserError(
                "Hedge-effectiveness test %s contains untrusted stored "
                "results; recompute it before upgrading." % test_id
            )
        test_results[test_id] = expected_effective

    cr.execute(
        """
        SELECT movement.id, movement.movement_date, latest.id,
               hedge.state, hedge.termination_date
          FROM eh_fx_hedge_movement movement
          JOIN eh_fx_hedge hedge ON hedge.id = movement.hedge_id
          LEFT JOIN LATERAL (
              SELECT test.id
                FROM eh_fx_hedge_test test
               WHERE test.hedge_id = hedge.id
                 AND test.test_date >= hedge.designation_date
                 AND test.test_date <= movement.movement_date
               ORDER BY test.test_date DESC, test.id DESC
               LIMIT 1
          ) latest ON TRUE
         WHERE movement.move_id IS NOT NULL
           AND movement.effective_portion != 0.0
         ORDER BY movement.id
        """
    )
    for (
        movement_id, movement_date, latest_test_id,
        hedge_state, termination_date,
    ) in cr.fetchall():
        closed_before_movement = (
            hedge_state in ('dedesignated', 'terminated')
            and (
                not termination_date
                or termination_date <= movement_date
            )
        )
        if (
            hedge_state == 'draft'
            or closed_before_movement
            or not latest_test_id
            or not test_results.get(latest_test_id)
        ):
            raise UserError(
                "Hedge movement %s defers a non-zero effective portion "
                "without an active designation and latest passing "
                "effectiveness test as of its movement date." % movement_id
            )


def _validate_revaluation_sources(cr):
    """Prove each stored support bucket from its exact period-end sources."""
    cr.execute(
        """
        SELECT support.id, run.id, run.aggregate_by_partner,
               run.company_id, run.revaluation_date,
               support.account_id, support.partner_id,
               support.foreign_currency_id, support.source_line_count,
               support.balance_company, support.balance_foreign,
               currency.rounding, company.currency_id,
               foreign_currency.rounding,
               rel.move_line_id, source.account_id, source.partner_id,
               source.currency_id, source.company_id, source.date,
               source_move.company_id, source_move.state, source_move.date,
               source.balance
                 - COALESCE((
                     SELECT SUM(partial.amount)
                       FROM account_partial_reconcile partial
                      WHERE partial.debit_move_id = source.id
                        AND partial.max_date <= run.revaluation_date
                   ), 0.0)
                 + COALESCE((
                     SELECT SUM(partial.amount)
                       FROM account_partial_reconcile partial
                      WHERE partial.credit_move_id = source.id
                        AND partial.max_date <= run.revaluation_date
                   ), 0.0) AS residual_company,
               source.amount_currency
                 - COALESCE((
                     SELECT SUM(partial.debit_amount_currency)
                       FROM account_partial_reconcile partial
                      WHERE partial.debit_move_id = source.id
                        AND partial.max_date <= run.revaluation_date
                   ), 0.0)
                 + COALESCE((
                     SELECT SUM(partial.credit_amount_currency)
                       FROM account_partial_reconcile partial
                      WHERE partial.credit_move_id = source.id
                        AND partial.max_date <= run.revaluation_date
                   ), 0.0) AS residual_foreign
          FROM eh_fx_revaluation_line support
          JOIN eh_fx_revaluation_run run ON run.id = support.run_id
          JOIN res_company company ON company.id = run.company_id
          JOIN res_currency currency ON currency.id = company.currency_id
          JOIN res_currency foreign_currency
            ON foreign_currency.id = support.foreign_currency_id
          LEFT JOIN eh_fx_reval_line_source_rel rel
            ON rel.reval_line_id = support.id
          LEFT JOIN account_move_line source
            ON source.id = rel.move_line_id
          LEFT JOIN account_move source_move
            ON source_move.id = source.move_id
         WHERE run.move_id IS NOT NULL
         ORDER BY run.id, support.id, rel.move_line_id
        """
    )
    supports = {}
    for row in cr.fetchall():
        supports.setdefault(row[0], []).append(row)

    claimed_sources = {}
    claimed_buckets = set()
    for support_id, rows in supports.items():
        first = rows[0]
        (
            _support_id, run_id, aggregate_by_partner, run_company_id,
            revaluation_date, support_account_id, support_partner_id,
            support_currency_id, source_line_count, raw_balance_company,
            raw_balance_foreign, raw_rounding, company_currency_id,
            raw_foreign_rounding,
        ) = first[:14]
        bucket_key = (
            run_id, support_account_id,
            support_partner_id if aggregate_by_partner else False,
            support_currency_id,
        )
        if bucket_key in claimed_buckets:
            raise UserError(
                "FX revaluation run %s retains duplicate support buckets."
                % run_id
            )
        claimed_buckets.add(bucket_key)
        if not source_line_count or len(rows) != source_line_count:
            raise UserError(
                "FX revaluation support %s does not retain its exact source "
                "journal-item count." % support_id
            )
        rounding = _finite_amount(
            raw_rounding, "FX revaluation currency rounding", run_id,
        )
        if rounding <= 0.0:
            raise UserError(
                "FX revaluation currency rounding is invalid on run %s."
                % run_id
            )
        foreign_rounding = _finite_amount(
            raw_foreign_rounding, "FX source currency rounding", run_id,
        )
        if foreign_rounding <= 0.0:
            raise UserError(
                "FX source currency rounding is invalid on run %s." % run_id
            )
        company_residuals = []
        foreign_residuals = []
        for row in rows:
            (
                source_id, source_account_id, source_partner_id,
                source_currency_id, source_company_projection,
                source_date_projection, source_company_id, source_state,
                source_date, raw_company_residual, raw_foreign_residual,
            ) = row[14:]
            if not source_id:
                raise UserError(
                    "FX revaluation support %s has missing source evidence."
                    % support_id
                )
            claim_key = (run_id, source_id)
            if claim_key in claimed_sources:
                raise UserError(
                    "FX source journal item %s is claimed more than once "
                    "inside run %s." % (source_id, run_id)
                )
            claimed_sources[claim_key] = support_id
            invalid_partner = (
                source_partner_id != support_partner_id
                if aggregate_by_partner else bool(support_partner_id)
            )
            if (
                source_account_id != support_account_id
                or invalid_partner
                or source_currency_id != support_currency_id
                or source_currency_id == company_currency_id
                or source_company_projection != run_company_id
                or source_company_projection != source_company_id
                or source_date_projection > revaluation_date
                or source_date_projection != source_date
                or source_company_id != run_company_id
                or source_state != 'posted'
                or source_date > revaluation_date
            ):
                raise UserError(
                    "FX revaluation support %s is not tied to its exact "
                    "posted source journal items." % support_id
                )
            company_residuals.append(_finite_amount(
                raw_company_residual, "FX source residual", support_id,
            ))
            foreign_residual = _finite_amount(
                raw_foreign_residual, "FX source residual", support_id,
            )
            if float_is_zero(
                foreign_residual, precision_rounding=foreign_rounding,
            ):
                raise UserError(
                    "FX revaluation support %s retains a source item that "
                    "was closed at the revaluation date." % support_id
                )
            foreign_residuals.append(foreign_residual)
        try:
            expected_company = float_round(
                math.fsum(company_residuals), precision_rounding=rounding,
            )
            expected_foreign = float_round(
                math.fsum(foreign_residuals), precision_digits=6,
            )
        except (OverflowError, ValueError) as exc:
            raise UserError(
                "FX source residuals are unsafe on support %s." % support_id
            ) from exc
        balance_company = _finite_amount(
            raw_balance_company, "FX revaluation support", support_id,
        )
        balance_foreign = _finite_amount(
            raw_balance_foreign, "FX revaluation support", support_id,
        )
        if (
            not math.isfinite(expected_company)
            or not math.isfinite(expected_foreign)
            or abs(balance_company - expected_company) >= 0.000001
            or abs(balance_foreign - expected_foreign) >= 0.000001
        ):
            raise UserError(
                "FX revaluation support %s does not match its period-end "
                "source residuals." % support_id
            )


def _validate_revaluation_moves(cr):
    _assert_no_rows(
        cr,
        """
        SELECT run.id
          FROM eh_fx_revaluation_run run
          JOIN account_move move ON move.id = run.move_id
         WHERE run.move_id IS NOT NULL
           AND (
               move.journal_id IS DISTINCT FROM run.journal_id
               OR move.date IS DISTINCT FROM run.revaluation_date
               OR move.reversed_entry_id IS NOT NULL
           )
         LIMIT 1
        """,
        "FX revaluation original move is not structurally tied to its run.",
    )
    _assert_no_rows(
        cr,
        """
        SELECT run.id
          FROM eh_fx_revaluation_run run
          JOIN account_move reversal ON reversal.id = run.reversal_move_id
         WHERE run.reversal_move_id IS NOT NULL
           AND (
               run.move_id IS NULL
               OR reversal.reversed_entry_id IS DISTINCT FROM run.move_id
               OR reversal.journal_id IS DISTINCT FROM run.journal_id
               OR reversal.date IS DISTINCT FROM run.reversal_date
           )
         LIMIT 1
        """,
        "FX reversal is not the exact reversal of its original move.",
    )
    _validate_revaluation_sources(cr)

    cr.execute(
        "SELECT run.id, run.move_id, run.reversal_move_id, "
        "run.gain_account_id, run.loss_account_id, currency.rounding "
        "FROM eh_fx_revaluation_run run "
        "JOIN res_company company ON company.id = run.company_id "
        "JOIN res_currency currency ON currency.id = company.currency_id "
        "WHERE run.move_id IS NOT NULL ORDER BY run.id"
    )
    runs = cr.fetchall()
    move_ids = {
        move_id
        for _, move_id, reversal_move_id, _, _, _ in runs
        for move_id in (move_id, reversal_move_id)
        if move_id
    }
    payloads = _fetch_move_payloads(cr, move_ids)
    run_ids = tuple(run[0] for run in runs)
    supports = {run_id: [] for run_id in run_ids}
    if run_ids:
        cr.execute(
            "SELECT support.run_id, support.account_id, support.partner_id, "
            "support.balance_company, support.balance_foreign, "
            "support.closing_rate, support.new_balance_company, "
            "support.adjustment, support.nature, account.account_type "
            "FROM eh_fx_revaluation_line support "
            "JOIN account_account account ON account.id = support.account_id "
            "WHERE support.run_id IN %s ORDER BY support.id",
            (run_ids,),
        )
        for row in cr.fetchall():
            supports[row[0]].append(row[1:])
    asset_types = {
        'asset_cash', 'asset_receivable', 'asset_current',
        'asset_non_current', 'asset_fixed', 'asset_prepayments',
    }
    for (
        run_id, move_id, reversal_move_id, gain_id, loss_id, raw_rounding,
    ) in runs:
        rounding = _finite_amount(
            raw_rounding, "FX revaluation currency rounding", run_id,
        )
        if rounding <= 0.0:
            raise UserError(
                "FX revaluation currency rounding is invalid on run %s."
                % run_id
            )
        expected = []
        gain_amounts = []
        loss_amounts = []
        for (
            account_id, partner_id, raw_balance_company,
            raw_balance_foreign, raw_closing_rate, raw_new_balance,
            raw_adjustment, nature, account_type,
        ) in supports.get(run_id, ()):
            balance_company = _finite_amount(
                raw_balance_company, "FX revaluation support", run_id,
            )
            balance_foreign = _finite_amount(
                raw_balance_foreign, "FX revaluation support", run_id,
            )
            closing_rate = _finite_amount(
                raw_closing_rate, "FX revaluation support", run_id,
            )
            if closing_rate <= 0.0:
                raise UserError(
                    "FX revaluation support closing rate is invalid on run "
                    "%s." % run_id
                )
            new_balance = _finite_amount(
                raw_new_balance, "FX revaluation support", run_id,
            )
            adjustment = _finite_amount(
                raw_adjustment, "FX revaluation support", run_id,
            )
            try:
                expected_new_balance = float_round(
                    balance_foreign * closing_rate,
                    precision_rounding=rounding,
                )
                expected_adjustment = float_round(
                    new_balance - balance_company,
                    precision_rounding=rounding,
                )
            except (OverflowError, ValueError) as exc:
                raise UserError(
                    "FX revaluation support arithmetic is unsafe on run %s."
                    % run_id
                ) from exc
            if (
                not math.isfinite(expected_new_balance)
                or not math.isfinite(expected_adjustment)
                or abs(new_balance - expected_new_balance) >= 0.000001
                or abs(adjustment - expected_adjustment) >= 0.000001
            ):
                raise UserError(
                    "FX revaluation support arithmetic is inconsistent on "
                    "run %s." % run_id
                )
            expected_nature = 'flat'
            if adjustment:
                if account_type in asset_types:
                    expected_nature = 'gain' if adjustment > 0.0 else 'loss'
                else:
                    expected_nature = 'loss' if adjustment < 0.0 else 'gain'
            if nature != expected_nature:
                raise UserError(
                    "FX revaluation support nature is inconsistent on "
                    "source %s." % run_id
                )
            if not adjustment:
                continue
            amount = abs(adjustment)
            if nature == 'gain':
                expected.append((account_id, partner_id or False, amount, 0.0))
                gain_amounts.append(amount)
            else:
                expected.append((account_id, partner_id or False, 0.0, amount))
                loss_amounts.append(amount)
        try:
            gain_total = math.fsum(gain_amounts)
            loss_total = math.fsum(loss_amounts)
        except OverflowError as exc:
            raise UserError(
                "FX revaluation support totals overflow on run %s." % run_id
            ) from exc
        if not math.isfinite(gain_total) or not math.isfinite(loss_total):
            raise UserError(
                "FX revaluation support totals are non-finite on run %s."
                % run_id
            )
        if gain_total:
            expected.append((gain_id, False, 0.0, gain_total))
        if loss_total:
            expected.append((loss_id, False, loss_total, 0.0))
        _assert_exact_payload(
            "FX revaluation", run_id, payloads.get(move_id, ()), expected,
        )
        if reversal_move_id:
            reversal_expected = [
                (account_id, partner_id, credit, debit)
                for account_id, partner_id, debit, credit
                in payloads.get(move_id, ())
            ]
            _assert_exact_payload(
                "FX reversal", run_id,
                payloads.get(reversal_move_id, ()), reversal_expected,
            )


def _validate_hedge_moves(cr):
    _assert_no_rows(
        cr,
        """
        SELECT movement.id
          FROM eh_fx_hedge_movement movement
          JOIN eh_fx_hedge hedge ON hedge.id = movement.hedge_id
          LEFT JOIN eh_fx_cta_position cta
            ON cta.id = hedge.cta_position_id
          JOIN account_move move ON move.id = movement.move_id
         WHERE movement.move_id IS NOT NULL
           AND (
               move.journal_id IS DISTINCT FROM hedge.journal_id
               OR move.date IS DISTINCT FROM movement.movement_date
               OR move.reversed_entry_id IS NOT NULL
           )
         LIMIT 1
        """,
        "FX hedge original move does not match its movement evidence.",
    )
    _assert_no_rows(
        cr,
        """
        SELECT movement.id
          FROM eh_fx_hedge_movement movement
          JOIN eh_fx_hedge hedge ON hedge.id = movement.hedge_id
          JOIN account_move move
            ON move.id = movement.reclassification_move_id
         WHERE movement.reclassification_move_id IS NOT NULL
           AND (
               movement.move_id IS NULL
               OR hedge.hedge_type = 'fair_value'
               OR (hedge.hedge_type = 'net_investment'
                   AND hedge.cta_position_id IS NOT NULL)
               OR move.journal_id IS DISTINCT FROM hedge.journal_id
               OR move.date IS DISTINCT FROM movement.reclassification_date
               OR move.reversed_entry_id IS NOT NULL
               OR movement.effective_portion = 0.0
           )
         LIMIT 1
        """,
        "FX hedge reclassification does not match its source movement.",
    )

    cr.execute(
        "SELECT movement.id, movement.move_id, "
        "movement.reclassification_move_id, movement.total_change, "
        "movement.effective_portion, movement.hedged_item_change, "
        "movement.ineffective_portion, "
        "hedge.hedge_type, hedge.instrument_account_id, "
        "hedge.oci_account_id, hedge.pl_account_id, "
        "hedge.hedged_item_account_id, hedge.cta_position_id, "
        "cta.cta_account_id "
        "FROM eh_fx_hedge_movement movement "
        "JOIN eh_fx_hedge hedge ON hedge.id = movement.hedge_id "
        "LEFT JOIN eh_fx_cta_position cta ON cta.id = hedge.cta_position_id "
        "WHERE movement.move_id IS NOT NULL "
        "OR movement.reclassification_move_id IS NOT NULL "
        "ORDER BY movement.id"
    )
    movements = cr.fetchall()
    move_ids = {
        move_id
        for row in movements
        for move_id in (row[1], row[2])
        if move_id
    }
    payloads = _fetch_move_payloads(cr, move_ids)
    for row in movements:
        (
            movement_id, move_id, reclass_id, raw_total, raw_effective,
            raw_hedged_item, raw_ineffective,
            hedge_type, instrument_id, oci_id, pl_id,
            hedged_item_id, cta_position_id, cta_account_id,
        ) = row
        total = _finite_amount(raw_total, "FX hedge movement", movement_id)
        effective = _finite_amount(
            raw_effective, "FX hedge movement", movement_id,
        )
        hedged_item = _finite_amount(
            raw_hedged_item, "FX hedge movement", movement_id,
        )
        ineffective = _finite_amount(
            raw_ineffective, "FX hedge movement", movement_id,
        )
        expected_ineffective = (
            total + hedged_item
            if hedge_type == 'fair_value' else total - effective
        )
        if (
            abs(ineffective - expected_ineffective) >= 0.000001
            or (
                effective and total
                and (effective > 0.0) != (total > 0.0)
            )
            or (
                hedged_item and total
                and (hedged_item > 0.0) == (total > 0.0)
            )
            or (
                hedge_type != 'fair_value'
                and abs(effective) - min(
                    abs(total), abs(hedged_item)
                ) >= 0.000001
            )
        ):
            raise UserError(
                "FX hedge movement arithmetic is inconsistent on source %s."
                % movement_id
            )
        expected = [_signed_payload_row(instrument_id, False, total)]
        if hedge_type == 'fair_value':
            if hedged_item:
                expected.append(_signed_payload_row(
                    hedged_item_id, False, hedged_item,
                ))
            if ineffective:
                expected.append(_signed_payload_row(
                    pl_id, False, -ineffective,
                ))
        else:
            effective_account_id = (
                cta_account_id
                if hedge_type == 'net_investment' and cta_position_id
                else oci_id
            )
            if effective:
                expected.append(_signed_payload_row(
                    effective_account_id, False, -effective,
                ))
            if ineffective:
                expected.append(_signed_payload_row(
                    pl_id, False, -ineffective,
                ))
        if move_id:
            _assert_exact_payload(
                "FX hedge movement", movement_id,
                payloads.get(move_id, ()), expected,
            )
        if reclass_id:
            reclass_expected = [
                _signed_payload_row(oci_id, False, effective),
                _signed_payload_row(pl_id, False, -effective),
            ]
            _assert_exact_payload(
                "FX hedge reclassification", movement_id,
                payloads.get(reclass_id, ()), reclass_expected,
            )


def _backfill_cta_provenance(cr):
    """Recover only CTA links proven by an engine-owned source relationship."""
    _assert_no_rows(
        cr,
        """
        SELECT disposal_move_id
          FROM eh_fx_cta_position
         WHERE disposal_move_id IS NOT NULL
         GROUP BY disposal_move_id
        HAVING COUNT(*) > 1
         LIMIT 1
        """,
        "One CTA disposal move is claimed by multiple positions.",
    )
    _assert_no_rows(
        cr,
        """
        SELECT pos.id
          FROM eh_fx_cta_position pos
          JOIN account_move move ON move.id = pos.disposal_move_id
         WHERE pos.disposal_move_id IS NOT NULL
           AND (
               pos.state != 'disposed'
               OR pos.disposal_pct IS DISTINCT FROM 100.0
               OR move.company_id IS DISTINCT FROM pos.company_id
               OR move.state IS DISTINCT FROM 'posted'
               OR move.eh_sealed IS NOT TRUE
               OR move.move_type IS DISTINCT FROM 'entry'
               OR move.journal_id IS DISTINCT FROM pos.journal_id
               OR move.reversed_entry_id IS NOT NULL
               OR (
                   SELECT COUNT(*) FROM account_move_line line
                    WHERE line.move_id = move.id
                      AND (line.debit != 0.0 OR line.credit != 0.0)
               ) != 2
               OR NOT EXISTS (
                   SELECT 1
                     FROM account_move_line debit_line
                     JOIN account_move_line credit_line
                       ON credit_line.move_id = debit_line.move_id
                      AND credit_line.id != debit_line.id
                    WHERE debit_line.move_id = move.id
                      AND debit_line.debit > 0.0
                      AND debit_line.credit = 0.0
                      AND credit_line.credit > 0.0
                      AND credit_line.debit = 0.0
                      AND ABS(
                          debit_line.debit - credit_line.credit
                      ) < 0.000001
                      AND (
                          (
                              debit_line.account_id = pos.cta_account_id
                              AND credit_line.account_id = pos.gain_account_id
                          ) OR (
                              debit_line.account_id = pos.loss_account_id
                              AND credit_line.account_id = pos.cta_account_id
                          )
                      )
               )
               OR (
                   move.eh_cta_position_id IS NOT NULL
                   AND move.eh_cta_position_id != pos.id
               )
           )
         LIMIT 1
        """,
        "CTA disposal source does not match its sealed reclassification.",
    )
    cr.execute(
        "SELECT pos.id, pos.disposal_move_id, pos.cta_account_id, "
        "pos.gain_account_id, pos.loss_account_id, currency.rounding "
        "FROM eh_fx_cta_position pos "
        "JOIN res_company company ON company.id = pos.company_id "
        "JOIN res_currency currency ON currency.id = company.currency_id "
        "WHERE pos.disposal_move_id IS NOT NULL ORDER BY pos.id"
    )
    positions = cr.fetchall()
    disposal_payloads = _fetch_move_payloads(
        cr, {row[1] for row in positions},
    )
    for (
        position_id, move_id, cta_id, gain_id, loss_id, raw_rounding,
    ) in positions:
        # Runtime CTA balance is credit-positive. Include already-tagged
        # reserve entries and legacy NIH movements whose exact source payload
        # was validated above, but never the disposal entry itself.
        cr.execute(
            "SELECT COALESCE(SUM(line.credit - line.debit), 0.0) "
            "FROM account_move_line line "
            "JOIN account_move move ON move.id = line.move_id "
            "WHERE line.account_id = %s AND move.state = 'posted' "
            "AND move.id != %s AND (move.eh_cta_position_id = %s OR EXISTS ("
            "SELECT 1 FROM eh_fx_hedge_movement movement "
            "JOIN eh_fx_hedge hedge ON hedge.id = movement.hedge_id "
            "WHERE movement.move_id = move.id "
            "AND hedge.hedge_type = 'net_investment' "
            "AND hedge.cta_position_id = %s))",
            (cta_id, move_id, position_id, position_id),
        )
        reserve = _finite_amount(
            cr.fetchone()[0], "CTA accumulated reserve", position_id,
        )
        if abs(reserve) < 0.000001:
            raise UserError(
                "CTA disposal source has no accumulated reserve on "
                "position %s." % position_id
            )
        rounding = _finite_amount(
            raw_rounding, "CTA company-currency rounding", position_id,
        )
        if rounding <= 0.0:
            raise UserError(
                "CTA company-currency rounding is invalid on position %s."
                % position_id
            )
        amount = float_round(abs(reserve), precision_rounding=rounding)
        if not math.isfinite(amount) or amount <= 0.0:
            raise UserError(
                "CTA disposal amount is invalid on position %s."
                % position_id
            )
        expected = (
            [
                _signed_payload_row(cta_id, False, amount),
                _signed_payload_row(gain_id, False, -amount),
            ]
            if reserve > 0.0 else
            [
                _signed_payload_row(loss_id, False, amount),
                _signed_payload_row(cta_id, False, -amount),
            ]
        )
        _assert_exact_payload(
            "CTA disposal", position_id,
            disposal_payloads.get(move_id, ()), expected,
        )
    cr.execute("""
        UPDATE account_move move
           SET eh_cta_position_id = pos.id
          FROM eh_fx_cta_position pos
         WHERE pos.disposal_move_id = move.id
           AND move.eh_cta_position_id IS NULL
    """)

    _assert_no_rows(
        cr,
        """
        SELECT movement.id
          FROM eh_fx_hedge_movement movement
          JOIN eh_fx_hedge hedge ON hedge.id = movement.hedge_id
          JOIN account_move move ON move.id = movement.move_id
         WHERE movement.move_id IS NOT NULL
           AND hedge.hedge_type = 'net_investment'
           AND hedge.cta_position_id IS NOT NULL
           AND movement.effective_portion != 0.0
           AND move.eh_cta_position_id IS NOT NULL
           AND move.eh_cta_position_id != hedge.cta_position_id
         LIMIT 1
        """,
        "Net-investment hedge move carries conflicting CTA provenance.",
    )
    cr.execute("""
        UPDATE account_move move
           SET eh_cta_position_id = hedge.cta_position_id
          FROM eh_fx_hedge_movement movement
          JOIN eh_fx_hedge hedge ON hedge.id = movement.hedge_id
         WHERE movement.move_id = move.id
           AND hedge.hedge_type = 'net_investment'
           AND hedge.cta_position_id IS NOT NULL
           AND movement.effective_portion != 0.0
           AND move.eh_cta_position_id IS NULL
    """)


def _validate_cta_moves(cr):
    _assert_no_rows(
        cr,
        """
        SELECT move.id
          FROM account_move move
          JOIN eh_fx_cta_position pos
            ON pos.id = move.eh_cta_position_id
         WHERE move.eh_cta_position_id IS NOT NULL
           AND (
               move.company_id IS DISTINCT FROM pos.company_id
               OR move.state IS DISTINCT FROM 'posted'
               OR move.eh_sealed IS NOT TRUE
               OR move.move_type IS DISTINCT FROM 'entry'
               OR NOT EXISTS (
                   SELECT 1 FROM account_move_line line
                    WHERE line.move_id = move.id
                      AND line.account_id = pos.cta_account_id
                      AND (line.debit != 0.0 OR line.credit != 0.0)
               )
               OR (
                   move.eh_fx_hedge_movement_id IS NOT NULL
                   AND NOT EXISTS (
                       SELECT 1
                         FROM eh_fx_hedge_movement movement
                         JOIN eh_fx_hedge hedge
                           ON hedge.id = movement.hedge_id
                        WHERE movement.id = move.eh_fx_hedge_movement_id
                          AND hedge.hedge_type = 'net_investment'
                          AND hedge.cta_position_id = pos.id
                          AND movement.move_id = move.id
                   )
               )
               OR (
                   pos.disposal_move_id = move.id
                   AND (
                       move.journal_id IS DISTINCT FROM pos.journal_id
                       OR (
                           SELECT COUNT(*) FROM account_move_line line
                            WHERE line.move_id = move.id
                              AND (line.debit != 0.0 OR line.credit != 0.0)
                       ) != 2
                       OR NOT EXISTS (
                           SELECT 1 FROM account_move_line line
                            WHERE line.move_id = move.id
                              AND line.account_id IN (
                                  pos.gain_account_id, pos.loss_account_id
                              )
                              AND (line.debit != 0.0 OR line.credit != 0.0)
                       )
                   )
               )
           )
         LIMIT 1
        """,
        "CTA-tagged move is not valid reserve/disposal evidence, including "
        "on an open position.",
    )
    _assert_no_rows(
        cr,
        """
        SELECT pos.id
          FROM eh_fx_cta_position pos
          LEFT JOIN account_move move ON move.id = pos.disposal_move_id
         WHERE pos.state = 'disposed'
           AND (
               move.id IS NULL
               OR move.eh_cta_position_id IS DISTINCT FROM pos.id
               OR move.company_id IS DISTINCT FROM pos.company_id
               OR move.state IS DISTINCT FROM 'posted'
               OR move.eh_sealed IS NOT TRUE
           )
         LIMIT 1
        """,
        "Disposed CTA position has inconsistent journal provenance.",
    )


def migrate_fx_provenance(cr):
    _validate_account_types(cr)
    _validate_effectiveness_evidence(cr)

    _assert_no_rows(
        cr,
        "SELECT id FROM eh_fx_revaluation_run "
        "WHERE state IN ('posted', 'reversed') AND move_id IS NULL LIMIT 1",
        "Posted FX revaluation run has no original journal entry.",
    )
    _assert_no_rows(
        cr,
        "SELECT id FROM eh_fx_revaluation_run "
        "WHERE state = 'reversed' AND reversal_move_id IS NULL LIMIT 1",
        "Reversed FX revaluation run has no reversal journal entry.",
    )
    _assert_no_rows(
        cr,
        "SELECT id FROM eh_fx_revaluation_run WHERE "
        "(move_id IS NOT NULL AND state NOT IN ('posted', 'reversed')) OR "
        "(reversal_move_id IS NOT NULL AND state != 'reversed') LIMIT 1",
        "FX revaluation journal links contradict the run state.",
    )
    _assert_no_rows(
        cr,
        "SELECT id FROM eh_fx_revaluation_run WHERE move_id IS NOT NULL "
        "AND move_id = reversal_move_id LIMIT 1",
        "FX revaluation source and reversal point to the same journal entry.",
    )
    _validate_revaluation_moves(cr)
    _backfill_link(
        cr, 'eh_fx_revaluation_run', 'move_id',
        'eh_fx_revaluation_run_id', 'FX revaluation', '', 'src.company_id')
    _backfill_link(
        cr, 'eh_fx_revaluation_run', 'reversal_move_id',
        'eh_fx_revaluation_reversal_run_id', 'FX reversal', '',
        'src.company_id')
    _assert_no_rows(
        cr,
        "SELECT id FROM account_move WHERE eh_fx_revaluation_run_id "
        "IS NOT NULL AND eh_fx_revaluation_reversal_run_id IS NOT NULL "
        "LIMIT 1",
        "One journal entry carries both FX source and reversal provenance.",
    )

    _assert_no_rows(
        cr,
        "SELECT id FROM eh_fx_hedge_movement "
        "WHERE state IN ('posted', 'reclassified') AND move_id IS NULL "
        "LIMIT 1",
        "Posted hedge movement has no original journal entry.",
    )
    _assert_no_rows(
        cr,
        "SELECT id FROM eh_fx_hedge_movement "
        "WHERE state = 'reclassified' AND reclassification_move_id IS NULL "
        "LIMIT 1",
        "Reclassified hedge movement has no reclassification entry.",
    )
    _assert_no_rows(
        cr,
        "SELECT id FROM eh_fx_hedge_movement WHERE "
        "(move_id IS NOT NULL AND state NOT IN ('posted', 'reclassified')) "
        "OR (reclassification_move_id IS NOT NULL "
        "AND state != 'reclassified') LIMIT 1",
        "FX hedge journal links contradict the movement state.",
    )
    _assert_no_rows(
        cr,
        "SELECT id FROM eh_fx_hedge_movement WHERE move_id IS NOT NULL "
        "AND move_id = reclassification_move_id LIMIT 1",
        "FX hedge source and reclassification point to the same journal entry.",
    )
    _validate_hedge_moves(cr)
    company_join = "JOIN eh_fx_hedge hedge ON hedge.id = src.hedge_id"
    _backfill_link(
        cr, 'eh_fx_hedge_movement', 'move_id',
        'eh_fx_hedge_movement_id', 'FX hedge movement',
        company_join, 'hedge.company_id')
    _backfill_link(
        cr, 'eh_fx_hedge_movement', 'reclassification_move_id',
        'eh_fx_hedge_reclassification_movement_id',
        'FX hedge reclassification', company_join, 'hedge.company_id')
    _assert_no_rows(
        cr,
        "SELECT id FROM account_move WHERE eh_fx_hedge_movement_id "
        "IS NOT NULL AND eh_fx_hedge_reclassification_movement_id "
        "IS NOT NULL LIMIT 1",
        "One journal entry carries both FX hedge source and "
        "reclassification provenance.",
    )
    _backfill_cta_provenance(cr)
    _validate_cta_moves(cr)


class _FxMigrationDryRunRollback(Exception):
    """Internal sentinel used to roll a successful diagnostic back."""


def diagnose_fx_provenance(cr):
    """Run every fail-closed check without persisting any backfill writes.

    The result is deliberately serialisable so upgrade tooling can display it
    before maintenance starts.  Both success and failure leave the database
    unchanged; a failure includes an actionable remediation sequence and the
    validator's exact first-conflict evidence.
    """
    try:
        try:
            with cr.savepoint():
                migrate_fx_provenance(cr)
                raise _FxMigrationDryRunRollback()
        except _FxMigrationDryRunRollback:
            return {
                'ok': True,
                'error': False,
                'remediation': (
                    "No remediation required. Run the upgrade against the "
                    "same database snapshot."
                ),
            }
    except UserError as exc:
        return {
            'ok': False,
            'error': str(exc),
            'remediation': (
                "Inspect the first conflicting record named above; restore "
                "its source-to-journal link and exact posted payload, or "
                "reverse and recreate it through the supported workflow. "
                "Then rerun diagnose_fx_provenance before upgrading."
            ),
        }
