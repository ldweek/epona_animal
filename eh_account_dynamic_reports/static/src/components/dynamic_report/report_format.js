/** @odoo-module **/
// ============================================================================
// ERP Heritage
// Copyright (C) 2026 (https://www.erpheritage.com.au/)
// ============================================================================
//
// Pure presentation helpers for the dynamic report viewer.
//
// These are stateless functions and constants: date-preset maths, the
// account-type label table, and the currency/figure formatter. They are
// split out of the viewer component so the component carries only stateful
// behaviour, and so this logic can be reused and reasoned about on its own.

import { _t } from "@web/core/l10n/translation";

function isoDate(d) {
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
}

function endOfMonth(date) {
    return new Date(date.getFullYear(), date.getMonth() + 1, 0);
}

function dateFromIso(value) {
    const [year, month, day] = String(value || "").split("-").map(Number);
    return new Date(year, month - 1, day);
}

function clampedDate(year, monthIndex, day) {
    const last = new Date(year, monthIndex + 1, 0).getDate();
    return new Date(year, monthIndex, Math.min(day, last));
}

function shiftMonths(date, count) {
    return new Date(
        date.getFullYear(), date.getMonth() + count, date.getDate(),
    );
}

function startOfQuarter(date) {
    const q = Math.floor(date.getMonth() / 3);
    return new Date(date.getFullYear(), q * 3, 1);
}

function endOfQuarter(date) {
    const q = Math.floor(date.getMonth() / 3);
    return new Date(date.getFullYear(), q * 3 + 3, 0);
}

export function todayStr() {
    return isoDate(new Date());
}

export function firstOfMonthStr() {
    const d = new Date();
    return isoDate(new Date(d.getFullYear(), d.getMonth(), 1));
}

// Server exceptions Odoo raises for the user to read (its error_dialogs
// registry shows them as warnings). Any other server exception keeps the
// generic title plus its class name, so a crash never prints its internals.
const USER_FACING_EXCEPTIONS = new Set([
    "odoo.exceptions.AccessDenied",
    "odoo.exceptions.AccessError",
    "odoo.exceptions.MissingError",
    "odoo.exceptions.RedirectWarning",
    "odoo.exceptions.UserError",
    "odoo.exceptions.ValidationError",
    "odoo.addons.web.controllers.action.MissingActionError",
    "odoo.addons.base.models.ir_actions.ServerActionWithWarningsError",
]);

// A failed server call carries the generic "Odoo Server Error" as its
// message; the text the user can act on is in data.message (data.arguments
// for a RedirectWarning). Prefer it so the viewer never hides why a
// report could not load.
export function errorMessage(error) {
    const fallback = (error && error.message) || String(error);
    const data = error && error.data;
    if (!data || typeof data !== "object" || typeof data.name !== "string") {
        return fallback;
    }
    const exceptionClass = (data.context && data.context.exception_class) || "";
    if (USER_FACING_EXCEPTIONS.has(data.name)
            || USER_FACING_EXCEPTIONS.has(exceptionClass)) {
        const first = Array.isArray(data.arguments) ? data.arguments[0] : null;
        const text = data.name === "odoo.exceptions.RedirectWarning"
            && typeof first === "string" ? first : data.message;
        return typeof text === "string" && text.trim() ? text : fallback;
    }
    return `${fallback} (${data.name.split(".").pop()})`;
}

export function fiscalYearRange(reference = new Date(), config = {}) {
    const endMonth = Math.min(12, Math.max(
        1, Number(config.lastMonth || config.fiscalyear_last_month || 12),
    ));
    const endDay = Math.max(
        1, Number(config.lastDay || config.fiscalyear_last_day || 31),
    );
    let end = clampedDate(reference.getFullYear(), endMonth - 1, endDay);
    if (reference > end) {
        end = clampedDate(reference.getFullYear() + 1, endMonth - 1, endDay);
    }
    const priorEnd = clampedDate(
        end.getFullYear() - 1, endMonth - 1, endDay,
    );
    const start = new Date(priorEnd);
    start.setDate(start.getDate() + 1);
    return [isoDate(start), isoDate(end)];
}

export function fiscalQuarterRange(reference = new Date(), config = {}) {
    const [fyFrom, fyTo] = fiscalYearRange(reference, config);
    const start = dateFromIso(fyFrom);
    const end = dateFromIso(fyTo);
    const monthOffset = (
        (reference.getFullYear() - start.getFullYear()) * 12
        + reference.getMonth() - start.getMonth()
    );
    const quarter = Math.max(0, Math.min(3, Math.floor(monthOffset / 3)));
    const quarterStart = new Date(
        start.getFullYear(), start.getMonth() + quarter * 3, 1,
    );
    const quarterEnd = new Date(
        quarterStart.getFullYear(), quarterStart.getMonth() + 3, 0,
    );
    return [isoDate(quarterStart), isoDate(quarterEnd > end ? end : quarterEnd)];
}

export function shiftDateRange(dateFrom, dateTo, direction, mode = "range") {
    const from = dateFromIso(dateFrom);
    const to = dateFromIso(dateTo);
    const step = direction < 0 ? -1 : 1;
    if (mode === "as_of") {
        to.setDate(to.getDate() + step);
        return [isoDate(to), isoDate(to)];
    }
    const monthModes = new Set(["this_month", "last_month"]);
    const quarterModes = new Set([
        "this_quarter", "last_quarter", "this_fiscal_quarter",
    ]);
    const yearModes = new Set([
        "this_year", "last_year", "this_fiscal_year",
    ]);
    if (monthModes.has(mode) || quarterModes.has(mode)) {
        const months = monthModes.has(mode) ? step : 3 * step;
        const shiftedStart = new Date(
            from.getFullYear(), from.getMonth() + months, 1,
        );
        const shiftedEnd = new Date(
            shiftedStart.getFullYear(),
            shiftedStart.getMonth() + (monthModes.has(mode) ? 1 : 3),
            0,
        );
        return [isoDate(shiftedStart), isoDate(shiftedEnd)];
    }
    if (yearModes.has(mode)) {
        return [
            isoDate(clampedDate(
                from.getFullYear() + step, from.getMonth(), from.getDate(),
            )),
            isoDate(clampedDate(
                to.getFullYear() + step, to.getMonth(), to.getDate(),
            )),
        ];
    }
    const duration = Math.max(0, Math.round((to - from) / 86400000));
    const delta = (duration + 1) * step;
    from.setDate(from.getDate() + delta);
    to.setDate(to.getDate() + delta);
    return [isoDate(from), isoDate(to)];
}

export const PRESET_RANGES = {
    this_month: () => {
        const today = new Date();
        return [isoDate(new Date(today.getFullYear(), today.getMonth(), 1)),
                isoDate(endOfMonth(today))];
    },
    last_month: () => {
        const today = new Date();
        const start = new Date(today.getFullYear(), today.getMonth() - 1, 1);
        return [isoDate(start), isoDate(endOfMonth(start))];
    },
    this_quarter: () => {
        const today = new Date();
        return [isoDate(startOfQuarter(today)), isoDate(endOfQuarter(today))];
    },
    last_quarter: () => {
        const today = new Date();
        const last = shiftMonths(today, -3);
        return [isoDate(startOfQuarter(last)), isoDate(endOfQuarter(last))];
    },
    this_year: () => {
        const today = new Date();
        return [`${today.getFullYear()}-01-01`, `${today.getFullYear()}-12-31`];
    },
    last_year: () => {
        const today = new Date();
        const y = today.getFullYear() - 1;
        return [`${y}-01-01`, `${y}-12-31`];
    },
    this_fiscal_year: (config) => fiscalYearRange(new Date(), config),
    this_fiscal_quarter: (config) => fiscalQuarterRange(new Date(), config),
};

export const ACCOUNT_TYPE_CHOICES = [
    { code: "asset_receivable", label: _t("Receivable") },
    { code: "asset_cash", label: _t("Cash") },
    { code: "asset_current", label: _t("Current Asset") },
    { code: "asset_non_current", label: _t("Non-current Asset") },
    { code: "asset_prepayments", label: _t("Prepayments") },
    { code: "asset_fixed", label: _t("Fixed Asset") },
    { code: "liability_payable", label: _t("Payable") },
    { code: "liability_credit_card", label: _t("Credit Card") },
    { code: "liability_current", label: _t("Current Liability") },
    { code: "liability_non_current", label: _t("Non-current Liability") },
    { code: "equity", label: _t("Equity") },
    { code: "equity_unaffected", label: _t("Current Year Earnings") },
    { code: "income", label: _t("Income") },
    { code: "income_other", label: _t("Other Income") },
    { code: "expense", label: _t("Expense") },
    // EH_ODOO19_EXPENSE_OTHER_START
    { code: "expense_other", label: _t("Other Expense") },
    // EH_ODOO19_EXPENSE_OTHER_END
    { code: "expense_depreciation", label: _t("Depreciation") },
    { code: "expense_direct_cost", label: _t("Cost of Revenue") },
    { code: "off_balance", label: _t("Off-balance") },
];

// Report controls are capabilities, not global decoration. Hiding controls
// whose handler cannot honour them prevents saved-view/cache noise and, more
// importantly, avoids figures that appear filtered or compared when backend
// semantics are unchanged. Keep this pure so Hoot can verify every report.
export const REPORT_CAPABILITIES = Object.freeze({
    executive_summary: ["comparison", "account_types", "nperiod"],
    trial_balance: [
        "comparison", "account_types", "nperiod", "analytic_columns",
    ],
    profit_and_loss: [
        "comparison", "account_types", "nperiod", "pivot",
        "analytic_columns",
    ],
    balance_sheet: [
        "comparison", "account_types", "nperiod", "analytic_columns",
    ],
    general_ledger: ["account_types"],
    partner_ledger: ["account_types"],
    aged_receivable: ["account_types"],
    aged_payable: ["account_types"],
    cash_flow: ["account_types", "recon", "method"],
    customer_statement: ["account_types"],
    vendor_statement: ["account_types"],
    analytic_balance: ["account_types"],
});

export function reportCapabilitiesForCode(code) {
    return REPORT_CAPABILITIES[code] || [];
}

const REPORT_UNSUPPORTED_OPTIONS = Object.freeze({
    // Bank-statement evidence cannot be partitioned by these GL-only
    // dimensions without changing one side of the reconciliation proof.
    bank_reconciliation: [
        "partner_ids", "account_ids", "account_type_ids",
        "analytic_account_ids", "analytic_plan_ids",
        "presentation_currency_id", "show_zero",
    ],
    deferred_revenue: [
        "journal_ids", "partner_ids", "account_ids", "account_type_ids",
        "analytic_account_ids", "analytic_plan_ids", "show_zero",
    ],
    deferred_expense: [
        "journal_ids", "partner_ids", "account_ids", "account_type_ids",
        "analytic_account_ids", "analytic_plan_ids", "show_zero",
    ],
});

export function unsupportedOptionsForCode(code) {
    return REPORT_UNSUPPORTED_OPTIONS[code] || [];
}

const DRILLDOWN_LINE_RULES = Object.freeze({
    trial_balance: /^account-\d+$/,
    profit_and_loss: /^account-\d+$/,
    balance_sheet: /^account-\d+$/,
    general_ledger: /^(account|aml)-\d+$/,
    partner_ledger: /^(partner|aml)-\d+$/,
    aged_receivable: /^(?:aml-\d+|partner-(?:\d+|none))$/,
    aged_payable: /^(?:aml-\d+|partner-(?:\d+|none))$/,
    customer_statement: /^aml-\d+$/,
    vendor_statement: /^aml-\d+$/,
    deferred_revenue: /^asset-\d+$/,
    deferred_expense: /^asset-\d+$/,
    bank_reconciliation: /^outstanding-\d+$/,
});

export function isDrillableReportLine(code, line) {
    if (!line || typeof line.id !== "string") return false;
    if ((line.meta || {}).kind === "account_aggregate") {
        return /^line-\d+$/.test(line.id);
    }
    if (code === "executive_summary") {
        return ["exec-cash", "exec-receivables", "exec-payables"].includes(
            line.id,
        );
    }
    const rule = DRILLDOWN_LINE_RULES[code];
    return Boolean(rule && rule.test(line.id));
}

export function lineCellHasValue(line, column, valueColumnDefs = []) {
    if (!line || !Array.isArray(line.columns) || !column) return false;
    let index = valueColumnDefs.indexOf(column);
    if (index < 0 && column.expression_label) {
        index = valueColumnDefs.findIndex(
            (candidate) => candidate.expression_label === column.expression_label,
        );
    }
    if (index < 0 || index >= line.columns.length) return false;
    const value = line.columns[index] && line.columns[index].value;
    return value !== null && value !== undefined && value !== "";
}

const NON_DRILLDOWN_EXPRESSIONS = new Set([
    "variance", "variance_pct", "closing_variance",
    "budget", "budget_variance",
]);

const SCOPED_ID_KEYS = [
    "company_ids", "analytic_account_ids", "analytic_plan_ids",
];

function isPositiveInteger(value) {
    return Number.isInteger(value) && value > 0;
}

export function normalizePositiveIdList(value) {
    if (!Array.isArray(value)) return [];
    const result = [];
    const seen = new Set();
    for (const rawId of value) {
        let id = rawId;
        if (typeof rawId === "string" && /^[1-9]\d*$/.test(rawId.trim())) {
            id = Number(rawId.trim());
        }
        if (!Number.isSafeInteger(id) || id <= 0 || seen.has(id)) continue;
        seen.add(id);
        result.push(id);
    }
    return result;
}

function isIsoDate(value) {
    if (typeof value !== "string"
            || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
        return false;
    }
    const [year, month, day] = value.split("-").map(Number);
    if (year < 1 || month < 1 || month > 12 || day < 1) return false;
    const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    const days = [31, leap ? 29 : 28, 31, 30, 31, 30,
        31, 31, 30, 31, 30, 31];
    return day <= days[month - 1];
}

function normalizeColumnScope(rawScope) {
    if (!rawScope || typeof rawScope !== "object" || Array.isArray(rawScope)) {
        return null;
    }
    const allowed = new Set([
        "date_from", "date_to", ...SCOPED_ID_KEYS,
        "comparison_index", "is_total",
    ]);
    if (Object.keys(rawScope).some((key) => !allowed.has(key))) {
        return null;
    }
    const scope = {};
    for (const key of ["date_from", "date_to"]) {
        if (key in rawScope) {
            if (!isIsoDate(rawScope[key])) return null;
            scope[key] = rawScope[key];
        }
    }
    // A one-sided accounting window is ambiguous. Snapshot handlers can
    // express their cumulative window explicitly as 0001-01-01..date_to.
    if (("date_from" in scope) !== ("date_to" in scope)) return null;
    if (scope.date_from && scope.date_from > scope.date_to) return null;
    for (const key of SCOPED_ID_KEYS) {
        if (!(key in rawScope)) continue;
        if (!Array.isArray(rawScope[key])
                || rawScope[key].some((id) => !isPositiveInteger(id))) {
            return null;
        }
        if (key === "company_ids" && !rawScope[key].length) return null;
        scope[key] = [...new Set(rawScope[key])];
    }
    if ("comparison_index" in rawScope) {
        if (!Number.isInteger(rawScope.comparison_index)
                || rawScope.comparison_index < 0
                || rawScope.comparison_index > 12) return null;
        scope.comparison_index = rawScope.comparison_index;
    }
    if ("is_total" in rawScope) {
        if (typeof rawScope.is_total !== "boolean") return null;
        scope.is_total = rawScope.is_total;
    }
    if (!Object.keys(scope).length) return null;
    return scope;
}

export function allocatedAnalyticScopeForColumn(column) {
    if (!column || !Object.prototype.hasOwnProperty.call(column, "scope")) {
        return null;
    }
    const scope = normalizeColumnScope(column.scope);
    if (!scope || scope.is_total !== false) return null;
    const accountIds = scope.analytic_account_ids || [];
    const planIds = scope.analytic_plan_ids || [];
    return accountIds.length || planIds.length ? scope : null;
}

const ANALYTIC_DRILLDOWN_KEYS = [
    "date", "move", "partner", "label", "allocated_amount",
];
const ANALYTIC_DRILLDOWN_TYPES = [
    "date", "string", "string", "string", "monetary",
];

/** Validate server-owned weighted analytic detail before rendering it. */
export function normalizeAnalyticDrilldownPage(rawPage) {
    if (!rawPage || typeof rawPage !== "object" || Array.isArray(rawPage)) {
        return null;
    }
    const columns = rawPage.columns;
    const rows = rawPage.rows;
    if (!Array.isArray(columns) || !Array.isArray(rows)
            || columns.length !== ANALYTIC_DRILLDOWN_KEYS.length) return null;
    const normalizedColumns = [];
    for (let index = 0; index < columns.length; index++) {
        const column = columns[index];
        if (!column || typeof column !== "object" || Array.isArray(column)
                || column.key !== ANALYTIC_DRILLDOWN_KEYS[index]
                || typeof column.name !== "string"
                || column.figure_type !== ANALYTIC_DRILLDOWN_TYPES[index]) {
            return null;
        }
        normalizedColumns.push({
            key: column.key,
            name: column.name,
            figure_type: column.figure_type,
        });
    }
    const normalizedRows = [];
    for (const row of rows) {
        if (!row || typeof row !== "object" || Array.isArray(row)
                || !(typeof row.id === "string" || isPositiveInteger(row.id))
                || !isPositiveInteger(row.move_line_id)
                || !isPositiveInteger(row.move_id)
                || !row.values || typeof row.values !== "object"
                || Array.isArray(row.values)) return null;
        const values = row.values;
        if (!isIsoDate(values.date)
                || typeof values.move !== "string"
                || typeof values.partner !== "string"
                || typeof values.label !== "string"
                || typeof values.allocated_amount !== "number"
                || !Number.isFinite(values.allocated_amount)) return null;
        normalizedRows.push({
            id: row.id,
            move_line_id: row.move_line_id,
            move_id: row.move_id,
            values: { ...values },
        });
    }
    const { offset, limit, total_count: totalCount } = rawPage;
    if (!Number.isInteger(offset) || offset < 0
            || !Number.isInteger(limit) || limit < 1 || limit > 500
            || !Number.isInteger(totalCount) || totalCount < 0
            || offset + normalizedRows.length > totalCount
            || typeof rawPage.has_more !== "boolean"
            || rawPage.has_more !== (
                offset + normalizedRows.length < totalCount
            )
            || typeof rawPage.total !== "number"
            || !Number.isFinite(rawPage.total)
            || typeof rawPage.page_token !== "string"
            || !/^[0-9a-f]{64}$/.test(rawPage.page_token)) return null;
    const currency = rawPage.currency;
    if (!currency || typeof currency !== "object" || Array.isArray(currency)
            || !isPositiveInteger(currency.id)
            || typeof currency.name !== "string"
            || typeof currency.symbol !== "string"
            || !["before", "after"].includes(currency.position)
            || !Number.isInteger(currency.decimal_places)
            || currency.decimal_places < 0
            || currency.decimal_places > 6) return null;
    const scope = normalizeColumnScope(rawPage.scope);
    if (!scope || scope.is_total !== false
            || !(
                (scope.analytic_account_ids || []).length
                || (scope.analytic_plan_ids || []).length
            )) return null;
    return {
        columns: normalizedColumns,
        rows: normalizedRows,
        total: rawPage.total,
        offset,
        limit,
        total_count: totalCount,
        has_more: rawPage.has_more,
        page_token: rawPage.page_token,
        currency: { ...currency },
        scope,
    };
}

export function drilldownOptionsForColumn(options, column, meta = {}) {
    const expression = column && column.expression_label;
    const expressionParts = typeof expression === "string"
        ? expression.split("__") : [];
    const semanticExpressions = new Set([
        expression,
        expressionParts[0],
        expressionParts[expressionParts.length - 1],
    ]);
    if (!expression
            || [...semanticExpressions].some(
                (part) => NON_DRILLDOWN_EXPRESSIONS.has(part),
            )) {
        return null;
    }
    const result = {
        ...(options || {}),
        date: { ...((options && options.date) || {}) },
        _eh_column_expression: expression,
    };
    if (column && Object.prototype.hasOwnProperty.call(column, "scope")) {
        const scope = normalizeColumnScope(column.scope);
        if (!scope) return null;
        if (scope.date_from) {
            result.date.date_from = scope.date_from;
            result.date.date_to = scope.date_to;
        }
        if ("company_ids" in scope) {
            result.company_ids = scope.company_ids.slice();
        }
        // Column slices must intersect, not replace, public row filters.
        // Private option keys let drilldown backend reproduce weighted
        // analytic allocation while preserving user's global filter axis.
        if ("analytic_account_ids" in scope) {
            result._eh_analytic_column_account_ids =
                scope.analytic_account_ids.slice();
        }
        if ("analytic_plan_ids" in scope) {
            result._eh_analytic_column_plan_ids =
                scope.analytic_plan_ids.slice();
        }
        result._eh_analytic_column_is_total = scope.is_total === true;
        if (scope.company_ids && scope.company_ids.length === 1) {
            result.primary_company_id = scope.company_ids[0];
        } else if (
            scope.company_ids
            && !scope.company_ids.includes(result.primary_company_id)
        ) {
            delete result.primary_company_id;
        }
        return result;
    }

    // Backward compatibility for cached payloads and third-party handlers
    // predating per-column scope metadata.  Scope above is always
    // authoritative when present; these fallbacks only decode contracts the
    // legacy renderer already published through report meta.
    const comparisonPeriods = Array.isArray(meta.comparison_periods)
        ? meta.comparison_periods : [];
    const applyLegacyPeriod = (period, snapshot = false) => {
        if (!period || !isIsoDate(period.from) || !isIsoDate(period.to)
                || period.from > period.to) return false;
        result.date.date_from = snapshot ? "0001-01-01" : period.from;
        result.date.date_to = period.to;
        return true;
    };
    if ([
        "prior_amount", "prior_value",
        "prior_closing_debit", "prior_closing_credit",
    ].includes(expression)) {
        const prior = {
            from: meta.prior_date_from || meta.prior_date_to,
            to: meta.prior_date_to,
        };
        if (!applyLegacyPeriod(prior, meta.date_basis === "as_of")) {
            return null;
        }
        return result;
    }
    let legacyMatch = expression.match(/^prior_([1-9][0-9]*)$/);
    if (legacyMatch) {
        const period = comparisonPeriods[Number(legacyMatch[1]) - 1];
        return applyLegacyPeriod(period, meta.date_basis === "as_of")
            ? result : null;
    }
    legacyMatch = expression.match(/^group_([1-9][0-9]*)$/);
    if (legacyMatch) {
        const companyId = (meta.company_ids || [])[Number(legacyMatch[1]) - 1];
        if (!isPositiveInteger(companyId)) return null;
        result.company_ids = [companyId];
        result.primary_company_id = companyId;
        return result;
    }

    // Unique period-axis/TB expressions use, for example,
    // amount__period_comparison_2__opening_debit.  Analytic suffixes cannot
    // be reconstructed truthfully (plans expand under ACL), so those remain
    // closed unless the authoritative scope branch above handled them.
    if (expressionParts.some((part) => part.startsWith("analytic_"))) {
        return null;
    }
    const currentPart = expressionParts.find(
        (part) => part === "period_current",
    );
    const comparisonPart = expressionParts.find(
        (part) => /^period_comparison_[1-9][0-9]*$/.test(part),
    );
    if (expression.includes("__")) {
        if (currentPart) {
            if (meta.date_basis === "as_of") {
                result.date.date_from = "0001-01-01";
            }
            return result;
        }
        if (comparisonPart) {
            const index = Number(comparisonPart.slice(
                "period_comparison_".length,
            ));
            const position = meta.comparison_order === "ascending"
                ? comparisonPeriods.length - index : index - 1;
            return applyLegacyPeriod(
                comparisonPeriods[position], meta.date_basis === "as_of",
            ) ? result : null;
        }
        return null;
    }

    legacyMatch = expression.match(/^period_([1-9][0-9]*)$/);
    if (legacyMatch) {
        const period = comparisonPeriods[Number(legacyMatch[1]) - 1];
        return applyLegacyPeriod(period, meta.date_basis === "as_of")
            ? result : null;
    }
    if (meta.date_basis === "as_of") {
        result.date.date_from = "0001-01-01";
    }
    return result;
}

/**
 * Validate optional grouped headers against the authoritative flat columns.
 *
 * Returns null for absent or malformed metadata; callers then render the
 * legacy single flat row. A valid grid has no overlaps or holes and spans
 * exactly columns.length cells across every physical header row.
 */
export function normalizeColumnHeaderRows(columns, rawRows) {
    if (!Array.isArray(columns) || !columns.length
            || !Array.isArray(rawRows) || !rawRows.length) return null;
    const height = rawRows.length;
    const width = columns.length;
    const occupied = Array.from({ length: height }, () => Array(width).fill(false));
    const normalized = [];
    for (let rowIndex = 0; rowIndex < height; rowIndex++) {
        const rawRow = rawRows[rowIndex];
        if (!Array.isArray(rawRow) || !rawRow.length) return null;
        const row = [];
        let cursor = 0;
        for (const rawCell of rawRow) {
            while (cursor < width && occupied[rowIndex][cursor]) cursor += 1;
            if (!rawCell || typeof rawCell !== "object"
                    || Array.isArray(rawCell)) return null;
            const colspan = rawCell.colspan === undefined ? 1 : rawCell.colspan;
            const rowspan = rawCell.rowspan === undefined ? 1 : rawCell.rowspan;
            if (!isPositiveInteger(colspan) || !isPositiveInteger(rowspan)
                    || cursor + colspan > width
                    || rowIndex + rowspan > height) return null;
            for (let y = rowIndex; y < rowIndex + rowspan; y++) {
                for (let x = cursor; x < cursor + colspan; x++) {
                    if (occupied[y][x]) return null;
                    occupied[y][x] = true;
                }
            }
            const name = rawCell.name === undefined ? "" : rawCell.name;
            if (!["string", "number"].includes(typeof name)
                    || (typeof name === "number" && !Number.isFinite(name))) {
                return null;
            }
            row.push({
                name: String(name), colspan, rowspan,
                start: cursor,
                key: `${rowIndex}-${cursor}-${colspan}-${rowspan}`,
            });
            cursor += colspan;
        }
        normalized.push(row);
    }
    if (occupied.some((row) => row.some((value) => !value))) return null;
    return normalized;
}

const NUMERIC_FIGURE_TYPES = new Set([
    "monetary", "integer", "float", "percentage",
]);

export function effectiveFigureType(lineColumn, columnDefinition) {
    return (lineColumn && lineColumn.figure_type)
        || (columnDefinition && columnDefinition.figure_type)
        || "string";
}

export function isNumericFigureType(figureType) {
    return NUMERIC_FIGURE_TYPES.has(figureType);
}

export function formatCurrency(value, currency, figureType, locale) {
    if (value === null || value === undefined || value === "") {
        return "";
    }
    if (typeof value !== "number") {
        return String(value);
    }
    // Odoo stores language codes with underscores (for example ``de_DE``),
    // while Intl expects BCP-47 hyphens. Normalise at the formatting edge so
    // the real ``user.context.lang`` never raises a RangeError.
    const numberLocale = locale
        ? String(locale).replace(/_/g, "-")
        : undefined;
    if (figureType === "integer") {
        return Math.trunc(value).toLocaleString(numberLocale);
    }
    if (figureType === "percentage") {
        const body = Math.abs(value * 100).toLocaleString(
            numberLocale,
            { minimumFractionDigits: 2, maximumFractionDigits: 2 },
        ) + "%";
        return value < 0 ? "(" + body + ")" : body;
    }
    if (figureType === "float") {
        const fixed = Math.abs(value).toLocaleString(numberLocale, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
        });
        return value < 0 ? "(" + fixed + ")" : fixed;
    }
    if (figureType !== "monetary") {
        return String(value);
    }
    const decimals = (currency && currency.decimal_places !== undefined)
        ? currency.decimal_places : 2;
    const fixed = Math.abs(value).toLocaleString(numberLocale, {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
    });
    let body;
    if (currency && currency.symbol && !currency.multi_currency) {
        if (currency.position === "before") {
            body = currency.symbol + " " + fixed;
        } else {
            body = fixed + " " + currency.symbol;
        }
    } else {
        body = fixed;
    }
    return value < 0 ? "(" + body + ")" : body;
}
