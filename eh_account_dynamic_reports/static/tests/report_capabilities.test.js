/** @odoo-module **/
// ============================================================================
// ERP Heritage
// Copyright (C) 2026 (https://www.erpheritage.com.au/)
// ============================================================================

import { describe, expect, test } from "@odoo/hoot";
import { mockDate } from "@odoo/hoot-mock";
import { patchTranslations } from "@web/../tests/web_test_helpers";
import { EhDynamicReportViewer } from "@eh_account_dynamic_reports/components/dynamic_report/dynamic_report";
import {
    PRESET_RANGES,
    allocatedAnalyticScopeForColumn,
    drilldownOptionsForColumn,
    effectiveFigureType,
    errorMessage,
    fiscalQuarterRange,
    fiscalYearRange,
    firstOfMonthStr,
    formatCurrency,
    isDrillableReportLine,
    lineCellHasValue,
    isNumericFigureType,
    normalizeAnalyticDrilldownPage,
    normalizeColumnHeaderRows,
    normalizePositiveIdList,
    reportCapabilitiesForCode,
    shiftDateRange,
    unsupportedOptionsForCode,
    todayStr,
} from "@eh_account_dynamic_reports/components/dynamic_report/report_format";

describe("eh dynamic report - per-cell figure types", () => {
    test("cell override drives mixed-column format and alignment semantics", () => {
        const genericColumn = { figure_type: "string" };
        const moneyType = effectiveFigureType(
            { value: 1250, figure_type: "monetary" }, genericColumn,
        );
        const percentType = effectiveFigureType(
            { value: 0.125, figure_type: "percentage" }, genericColumn,
        );
        expect(moneyType).toBe("monetary");
        expect(percentType).toBe("percentage");
        expect(isNumericFigureType(moneyType)).toBe(true);
        expect(formatCurrency(0.125, {}, percentType)).toBe("12.50%");
    });

    test("legacy cells fall back to column type and n/a stays visible", () => {
        expect(effectiveFigureType({}, { figure_type: "float" })).toBe("float");
        expect(effectiveFigureType({}, {})).toBe("string");
        expect(isNumericFigureType("string")).toBe(false);
        expect(formatCurrency("n/a", {}, "percentage")).toBe("n/a");
    });

    test("number formatting follows the Odoo user locale", () => {
        const currency = {
            symbol: "€", position: "after", decimal_places: 2,
            multi_currency: false,
        };
        // Pass the underscore-separated value Odoo stores in res.lang.
        expect(formatCurrency(1234.5, currency, "monetary", "de_DE"))
            .toBe("1.234,50 €");
        expect(formatCurrency(-0.125, {}, "percentage", "de_DE"))
            .toBe("(12,50%)");
    });
});

describe("eh dynamic report - local calendar presets", () => {
    test("positive UTC offsets never shift dates to prior day", () => {
        mockDate("2026-08-24T00:30:00", +10);
        expect(todayStr()).toBe("2026-08-24");
        expect(firstOfMonthStr()).toBe("2026-08-01");
        expect(PRESET_RANGES.this_month()).toEqual([
            "2026-08-01", "2026-08-31",
        ]);
        expect(PRESET_RANGES.this_quarter()).toEqual([
            "2026-07-01", "2026-09-30",
        ]);
    });

    test("fiscal presets and period navigation honour a June year end", () => {
        const reference = new Date(2026, 7, 24);
        expect(fiscalYearRange(reference, { lastMonth: 6, lastDay: 30 }))
            .toEqual(["2026-07-01", "2027-06-30"]);
        expect(fiscalQuarterRange(reference, { lastMonth: 6, lastDay: 30 }))
            .toEqual(["2026-07-01", "2026-09-30"]);
        expect(shiftDateRange(
            "2026-07-01", "2026-09-30", -1, "this_fiscal_quarter",
        )).toEqual(["2026-04-01", "2026-06-30"]);
    });
});

describe("eh dynamic report - viewer date and access safety", () => {
    test("as-of uses a bounded single day and refreshes immediately", () => {
        let refreshCount = 0;
        const viewer = {
            state: {
                options: {
                    date: {
                        mode: "range",
                        date_from: "2026-01-01",
                        date_to: "2026-12-31",
                    },
                },
            },
            refresh() {
                refreshCount += 1;
            },
        };
        EhDynamicReportViewer.prototype.onDateModeChange.call(
            viewer,
            { target: { value: "as_of" } },
        );
        expect(viewer.state.options.date).toEqual({
            mode: "as_of",
            date_from: "2026-12-31",
            date_to: "2026-12-31",
        });
        expect(refreshCount).toBe(1);
    });

    test("bootstrap turns a denied report lookup into a friendly state", async () => {
        const viewer = {
            reportCode: "profit_and_loss",
            user: { context: { allowed_company_ids: [10] } },
            state: {
                loading: true,
                error: null,
                options: { company_ids: [] },
            },
            orm: {
                async searchRead() {
                    throw new Error("Access denied");
                },
            },
        };
        await EhDynamicReportViewer.prototype.bootstrap.call(viewer);
        expect(viewer.state.loading).toBe(false);
        // Odoo 19 keeps this translation lazy in the isolated Hoot runtime;
        // read the String subclass' source value without forcing translation.
        expect(String.prototype.valueOf.call(viewer.state.error)).toBe(
            "You do not have access to this ERP Heritage report.",
        );
        expect(viewer.state.options.company_ids).toEqual([10]);
    });

    test("later refresh wins when responses arrive out of order", async () => {
        const pending = [];
        const viewer = {
            reportCode: null,
            _refreshSequence: 0,
            state: {
                reportId: 7,
                options: {},
                loading: false,
                error: null,
                payload: null,
                childLines: {},
                scrollTop: 0,
                tableQuery: "",
                expandedLines: [],
            },
            bodyRef: null,
            normalizeCapabilityOptions() {},
            onCloseAnalyticDrilldown() {},
            orm: {
                call() {
                    return new Promise((resolve) => pending.push(resolve));
                },
            },
        };
        const first = EhDynamicReportViewer.prototype.refresh.call(viewer);
        const second = EhDynamicReportViewer.prototype.refresh.call(viewer);
        pending[1]({ marker: "new", lines: [] });
        await second;
        pending[0]({ marker: "stale", lines: [] });
        await first;
        expect(viewer.state.payload.marker).toBe("new");
        expect(viewer.state.loading).toBe(false);
    });

    test("expand response landing after a refresh is dropped", async () => {
        const pending = [];
        const notes = [];
        const viewer = {
            reportCode: null,
            _refreshSequence: 0,
            _childLinesSequence: 0,
            state: {
                reportId: 7,
                options: {},
                loading: false,
                error: null,
                payload: null,
                childLines: {},
                scrollTop: 0,
                tableQuery: "",
                expandedLines: [],
            },
            bodyRef: null,
            normalizeCapabilityOptions() {},
            onCloseAnalyticDrilldown() {},
            notification: { add(message) { notes.push(message); } },
            orm: {
                call() {
                    return new Promise((resolve, reject) => {
                        pending.push({ resolve, reject });
                    });
                },
            },
        };
        const proto = EhDynamicReportViewer.prototype;
        const line = { id: "account-1", lazy: true };

        // Without a refresh in between, the page is kept as before.
        const kept = proto.loadChildren.call(viewer, line, 0);
        pending.pop().resolve({
            child_lines: [{ id: "aml-0", name: "current period" }],
            has_more: false,
        });
        await kept;
        expect(viewer.state.childLines["account-1"].lines.map((l) => l.id))
            .toEqual(["aml-0"]);

        // A refresh resolves while the expand is still in flight.
        const stale = proto.loadChildren.call(viewer, line, 0);
        const staleRpc = pending.pop();
        expect(viewer.state.childLines["account-1"].loading).toBe(true);
        const refreshed = proto.refresh.call(viewer);
        pending.pop().resolve({ lines: [] });
        await refreshed;
        expect(viewer.state.childLines).toEqual({});
        staleRpc.resolve({
            child_lines: [{ id: "aml-1", name: "old period" }],
            has_more: false,
        });
        await stale;
        expect(viewer.state.childLines).toEqual({});

        // A failure of an obsolete request writes nothing and stays silent.
        const failing = proto.loadChildren.call(viewer, line, 0);
        const failingRpc = pending.pop();
        const refreshedAgain = proto.refresh.call(viewer);
        pending.pop().resolve({ lines: [] });
        await refreshedAgain;
        failingRpc.reject(new Error("gone"));
        await failing;
        expect(viewer.state.childLines).toEqual({});
        expect(notes).toEqual([]);
    });

    test("clear all resets every counted advanced filter", () => {
        let refreshed = 0;
        const viewer = {
            state: { options: {
                journal_ids: [1], partner_ids: [2], account_ids: [3],
                account_type_ids: ["income"], analytic_account_ids: [4],
                analytic_plan_ids: [5], comparison: "previous_year",
                comparison_number: 3, horizontal_group_by: "company",
                comparison_custom_date_from: "2024-01-01",
                comparison_custom_date_to: "2024-12-31",
                comparison_order: "ascending",
                analytic_column_account_ids: [6],
                analytic_column_plan_ids: [7],
                cash_flow_method: "indirect", cash_flow_reconciled: true,
                cf_interest_paid_section: "financing",
                cf_dividends_paid_section: "operating",
                presentation_currency_id: 9, posted_only: false,
                show_zero: true, aging_interval: 45,
                aging_bucket_count: 7, aging_basis: "invoice_date",
                reconcile_state: "all",
            } },
            refresh() { refreshed += 1; },
        };
        EhDynamicReportViewer.prototype.onClearAllFilters.call(viewer);
        expect(viewer.state.options.presentation_currency_id).toBe(null);
        expect(viewer.state.options.posted_only).toBe(true);
        expect(viewer.state.options.show_zero).toBe(false);
        expect(viewer.state.options.aging_interval).toBe(30);
        expect(viewer.state.options.aging_bucket_count).toBe(4);
        expect(viewer.state.options.aging_basis).toBe("maturity");
        expect(viewer.state.options.reconcile_state).toBe("open");
        expect(viewer.state.options.comparison_custom_date_from).toBe("");
        expect(viewer.state.options.comparison_custom_date_to).toBe("");
        expect(viewer.state.options.comparison_order).toBe("descending");
        expect(viewer.state.options.analytic_column_account_ids).toEqual([]);
        expect(viewer.state.options.analytic_column_plan_ids).toEqual([]);
        expect(refreshed).toBe(1);
    });

    test("saved views preserve new period and analytic axis options", async () => {
        let refreshed = 0;
        const viewer = {
            state: {
                options: {
                    comparison: "none", comparison_number: 1,
                    comparison_custom_date_from: "",
                    comparison_custom_date_to: "",
                    comparison_order: "descending",
                    analytic_column_account_ids: [],
                    analytic_column_plan_ids: [],
                    hierarchical_groups: true,
                    unfold_all: false,
                    unfolded_lines: [],
                },
                currentSavedViewId: null,
            },
            orm: { async call() {
                return {
                    comparison: "custom", comparison_number: 1,
                    comparison_custom_date_from: "2025-01-01",
                    comparison_custom_date_to: "2025-12-31",
                    comparison_order: "ascending",
                    analytic_column_account_ids: [41],
                    analytic_column_plan_ids: [51],
                    hierarchical_groups: false,
                    unfold_all: true,
                    unfolded_lines: ["group-2", "group-1"],
                    ignored_future_key: true,
                };
            } },
            savedViewOptionDefaults:
                EhDynamicReportViewer.prototype.savedViewOptionDefaults,
            refresh() { refreshed += 1; },
            notification: { add() {} },
        };
        await EhDynamicReportViewer.prototype.onSavedViewChange.call(
            viewer, { target: { value: "8" } },
        );
        expect(viewer.state.options.comparison).toBe("custom");
        expect(viewer.state.options.comparison_custom_date_from)
            .toBe("2025-01-01");
        expect(viewer.state.options.comparison_order).toBe("ascending");
        expect(viewer.state.options.analytic_column_account_ids).toEqual([41]);
        expect(viewer.state.options.analytic_column_plan_ids).toEqual([51]);
        expect(viewer.state.options.hierarchical_groups).toBe(false);
        expect(viewer.state.options.unfold_all).toBe(true);
        expect(viewer.state.options.unfolded_lines)
            .toEqual(["group-2", "group-1"]);
        expect(viewer.state.options.ignored_future_key).toBe(undefined);
        expect(viewer.state.currentSavedViewId).toBe(8);
        expect(refreshed).toBe(1);
    });

    test("legacy saved views clear active axes and hierarchy state", async () => {
        let refreshed = 0;
        const viewer = {
            state: {
                options: {
                    date: {
                        date_from: "2026-01-01",
                        date_to: "2026-12-31",
                    },
                    company_ids: [3],
                    comparison: "custom", comparison_number: 4,
                    comparison_custom_date_from: "2025-01-01",
                    comparison_custom_date_to: "2025-12-31",
                    comparison_order: "ascending",
                    analytic_account_ids: [31], analytic_plan_ids: [32],
                    analytic_column_account_ids: [41],
                    analytic_column_plan_ids: [51],
                    hierarchical_groups: false,
                    unfold_all: true,
                    unfolded_lines: ["group-2", "group-1"],
                },
                currentSavedViewId: null,
            },
            orm: { async call() {
                return {
                    date: {
                        date_from: "2024-01-01",
                        date_to: "2024-12-31",
                    },
                    company_ids: [7],
                };
            } },
            savedViewOptionDefaults:
                EhDynamicReportViewer.prototype.savedViewOptionDefaults,
            refresh() { refreshed += 1; },
            notification: { add() {} },
        };
        await EhDynamicReportViewer.prototype.onSavedViewChange.call(
            viewer, { target: { value: "9" } },
        );
        expect(viewer.state.options.date.date_from).toBe("2024-01-01");
        expect(viewer.state.options.company_ids).toEqual([7]);
        expect(viewer.state.options.comparison).toBe("none");
        expect(viewer.state.options.comparison_number).toBe(1);
        expect(viewer.state.options.comparison_custom_date_from).toBe("");
        expect(viewer.state.options.comparison_custom_date_to).toBe("");
        expect(viewer.state.options.comparison_order).toBe("descending");
        expect(viewer.state.options.analytic_account_ids).toEqual([]);
        expect(viewer.state.options.analytic_plan_ids).toEqual([]);
        expect(viewer.state.options.analytic_column_account_ids).toEqual([]);
        expect(viewer.state.options.analytic_column_plan_ids).toEqual([]);
        expect(viewer.state.options.hierarchical_groups).toBe(true);
        expect(viewer.state.options.unfold_all).toBe(false);
        expect(viewer.state.options.unfolded_lines).toEqual([]);
        expect(viewer.state.currentSavedViewId).toBe(9);
        expect(refreshed).toBe(1);
    });

    test("saved axis options normalize stale comparison and analytic ids", () => {
        expect(normalizePositiveIdList(
            ["2", 3, 3, 0, -1, true, "bad", 4.5, {}, [5]],
        )).toEqual([2, 3]);
        expect(normalizePositiveIdList(7)).toEqual([]);
        const viewer = {
            reportCode: "trial_balance",
            state: { options: {
                comparison: "none", comparison_number: 7,
                comparison_custom_date_from: "2025-01-01",
                comparison_custom_date_to: "2025-12-31",
                comparison_order: "ascending",
                analytic_column_account_ids: ["2", 2, -1, 3],
                analytic_column_plan_ids: [4, "4", false, 5],
                horizontal_group_by: null,
            }, payload: null },
            supportsComparison: () => true,
            supportsAccountTypes: () => true,
            unsupportedOptions: () => [],
            supportsPivot: () => true,
            supportsAnalyticColumns: () => true,
            analyticColumnMax:
                EhDynamicReportViewer.prototype.analyticColumnMax,
            comparisonNumberMax:
                EhDynamicReportViewer.prototype.comparisonNumberMax,
            normalizeComparisonBudgetOptions:
                EhDynamicReportViewer.prototype.normalizeComparisonBudgetOptions,
        };
        EhDynamicReportViewer.prototype.normalizeCapabilityOptions.call(viewer);
        expect(viewer.state.options.comparison_number).toBe(1);
        expect(viewer.state.options.comparison_order).toBe("descending");
        expect(viewer.state.options.comparison_custom_date_from).toBe("");
        expect(viewer.state.options.comparison_custom_date_to).toBe("");
        expect(viewer.state.options.analytic_column_account_ids).toEqual([2, 3]);
        expect(viewer.state.options.analytic_column_plan_ids).toEqual([4, 5]);
    });

    test("custom comparison gets a complete deterministic default window", () => {
        let refreshed = 0;
        const viewer = { state: { options: {
            date: { date_from: "2026-01-01", date_to: "2026-12-31" },
            comparison: "none", comparison_number: 4,
            comparison_custom_date_from: "",
            comparison_custom_date_to: "",
            analytic_column_account_ids: [], analytic_column_plan_ids: [],
        } },
        reportCode: "profit_and_loss",
        comparisonNumberMax:
            EhDynamicReportViewer.prototype.comparisonNumberMax,
        normalizeComparisonBudgetOptions:
            EhDynamicReportViewer.prototype.normalizeComparisonBudgetOptions,
        refresh() { refreshed += 1; } };
        EhDynamicReportViewer.prototype.onComparisonChange.call(
            viewer, { target: { value: "custom" } },
        );
        expect(viewer.state.options.comparison_number).toBe(1);
        expect(viewer.state.options.comparison_custom_date_from)
            .toBe("2026-01-01");
        expect(viewer.state.options.comparison_custom_date_to)
            .toBe("2026-12-31");
        expect(refreshed).toBe(1);
    });

    test("analytic column picker enforces combined eight-group ceiling", () => {
        patchTranslations();
        let refreshed = 0;
        let warning = null;
        const viewer = {
            reportCode: "profit_and_loss",
            state: { options: {
                analytic_column_plan_ids: [1, 2, 3],
                analytic_column_account_ids: [],
                comparison: "none", comparison_number: 1,
            } },
            notification: { add(message) { warning = message; } },
            analyticColumnMax:
                EhDynamicReportViewer.prototype.analyticColumnMax,
            comparisonNumberMax:
                EhDynamicReportViewer.prototype.comparisonNumberMax,
            normalizeComparisonBudgetOptions:
                EhDynamicReportViewer.prototype.normalizeComparisonBudgetOptions,
            refresh() { refreshed += 1; },
        };
        EhDynamicReportViewer.prototype.onAnalyticColumnSelectChange.call(
            viewer,
            { target: { selectedOptions: [
                { value: "11" }, { value: "12" }, { value: "13" },
                { value: "14" }, { value: "15" }, { value: "16" },
            ] } },
            "analytic_column_account_ids",
        );
        expect(viewer.state.options.analytic_column_account_ids)
            .toEqual([11, 12, 13, 14, 15]);
        expect(warning).toBe("Select no more than 8 analytic columns.");
        expect(refreshed).toBe(1);
    });

    test("trial balance period picker respects 48-value-column ceiling", () => {
        let refreshed = 0;
        const viewer = {
            reportCode: "trial_balance",
            state: { options: {
                comparison: "previous_year", comparison_number: 1,
                analytic_column_account_ids: [],
                analytic_column_plan_ids: [],
            } },
            comparisonNumberMax:
                EhDynamicReportViewer.prototype.comparisonNumberMax,
            normalizeComparisonBudgetOptions:
                EhDynamicReportViewer.prototype.normalizeComparisonBudgetOptions,
            refresh() { refreshed += 1; },
        };
        EhDynamicReportViewer.prototype.onComparisonNumberChange.call(
            viewer, { target: { value: "12" } },
        );
        expect(viewer.state.options.comparison_number).toBe(7);
        expect(refreshed).toBe(1);
        for (const [groups, expected] of [
            [0, 7], [1, 3], [2, 1], [3, 1], [4, 0], [7, 0],
        ]) {
            expect(EhDynamicReportViewer.prototype.comparisonNumberMax.call({
                reportCode: "trial_balance",
                state: { options: {
                    analytic_column_account_ids: Array.from(
                        { length: groups }, (_, index) => index + 1,
                    ),
                    analytic_column_plan_ids: [],
                } },
            })).toBe(expected);
        }
        expect(
            EhDynamicReportViewer.prototype.comparisonControlsAvailable.call({
                supportsComparison: () => true,
                comparisonNumberMax: () => 0,
            }),
        ).toBe(false);
        expect(
            EhDynamicReportViewer.prototype.comparisonControlsAvailable.call({
                supportsComparison: () => true,
                comparisonNumberMax: () => 1,
            }),
        ).toBe(true);
        expect(EhDynamicReportViewer.prototype.comparisonNumberMax.call({
            reportCode: "profit_and_loss",
        })).toBe(12);
        expect(EhDynamicReportViewer.prototype.comparisonNumberMax.call({
            reportCode: "profit_and_loss",
            state: { options: {
                analytic_column_account_ids: [1, 2, 3, 4, 5],
                analytic_column_plan_ids: [6, 7, 8],
            } },
        })).toBe(4);
    });

    test("trial balance analytic cap removes impossible comparison", () => {
        patchTranslations();
        let warning = null;
        const viewer = {
            reportCode: "trial_balance",
            state: { options: {
                analytic_column_plan_ids: [1, 2, 3],
                analytic_column_account_ids: [],
                comparison: "previous_year", comparison_number: 3,
                comparison_custom_date_from: "2025-01-01",
                comparison_custom_date_to: "2025-12-31",
                comparison_order: "ascending",
            } },
            notification: { add(message) { warning = message; } },
            analyticColumnMax:
                EhDynamicReportViewer.prototype.analyticColumnMax,
            comparisonNumberMax:
                EhDynamicReportViewer.prototype.comparisonNumberMax,
            normalizeComparisonBudgetOptions:
                EhDynamicReportViewer.prototype.normalizeComparisonBudgetOptions,
            refresh() {},
        };
        EhDynamicReportViewer.prototype.onAnalyticColumnSelectChange.call(
            viewer,
            { target: { selectedOptions: [
                { value: "11" }, { value: "12" }, { value: "13" },
                { value: "14" }, { value: "15" },
            ] } },
            "analytic_column_account_ids",
        );
        expect(viewer.state.options.analytic_column_account_ids)
            .toEqual([11, 12, 13, 14]);
        expect(warning).toBe("Select no more than 7 analytic columns.");
        expect(viewer.state.options.comparison).toBe("none");
        expect(viewer.state.options.comparison_number).toBe(1);
        expect(viewer.state.options.comparison_order).toBe("descending");
        expect(EhDynamicReportViewer.prototype.comparisonNumberMax.call(viewer))
            .toBe(0);
    });

    test("row search includes already-loaded lazy children", () => {
        const getter = Object.getOwnPropertyDescriptor(
            EhDynamicReportViewer.prototype, "tableFilteredIds",
        ).get;
        const viewer = {
            state: {
                tableQuery: "hidden invoice",
                payload: { lines: [{ id: "account-1", name: "Receivable" }] },
                childLines: {
                    "account-1": { lines: [{
                        id: "aml-8", name: "Hidden invoice",
                        parent_id: "account-1",
                    }] },
                },
            },
            _filterCache: null,
        };
        const keep = getter.call(viewer);
        expect(keep.has("aml-8")).toBe(true);
        expect(keep.has("account-1")).toBe(true);
    });

    // A fake viewer over a section with two lazy accounts and one unrelated
    // lazy account, wired to the real prototype methods under test.
    const searchViewer = ({ query, expandedLines, childLines }) => {
        const proto = EhDynamicReportViewer.prototype;
        const viewer = {
            state: {
                tableQuery: query,
                expandedLines,
                childLines,
                payload: { lines: [
                    { id: "section-a", name: "Current Assets", unfoldable: true },
                    { id: "account-1", name: "1000 Cash", parent_id: "section-a",
                      lazy: true, unfoldable: true },
                    { id: "account-2", name: "1020 Bank", parent_id: "section-a",
                      lazy: true, unfoldable: true },
                    { id: "account-9", name: "2000 Payable",
                      lazy: true, unfoldable: true },
                ] },
            },
            _filterCache: null,
            _loadMoreSentinel: proto._loadMoreSentinel,
            visibleLines: proto.visibleLines,
        };
        for (const name of ["tableFilteredIds", "visibleCount"]) {
            Object.defineProperty(
                viewer, name, Object.getOwnPropertyDescriptor(proto, name),
            );
        }
        return viewer;
    };
    const visibleIds = (viewer) => viewer.visibleLines().map((l) => l.id);

    test("row search never adds a loading row under an unopened lazy leaf", () => {
        const viewer = searchViewer({
            query: "10", expandedLines: ["section-a"], childLines: {},
        });
        expect(visibleIds(viewer)).toEqual(["section-a", "account-1", "account-2"]);
        expect(viewer.visibleLines().some(
            (l) => l.meta && l.meta.kind === "load_more",
        )).toBe(false);
    });

    test("row search keeps fetched items of an opened leaf without parent_id", () => {
        const viewer = searchViewer({
            query: "1000",
            expandedLines: ["section-a", "account-1"],
            childLines: { "account-1": {
                lines: [{ id: "aml-8", name: "Opening float" }],
                hasMore: false, nextOffset: 1, totalCount: 1, loading: false,
            } },
        });
        expect(visibleIds(viewer)).toEqual(["section-a", "account-1", "aml-8"]);
    });

    test("collapsed lazy leaf reveals only fetched items that match", () => {
        const childLines = { "account-1": {
            lines: [
                { id: "aml-8", name: "INV/0042" },
                { id: "aml-9", name: "INV/0043" },
            ],
            hasMore: true, nextOffset: 2, totalCount: 9, loading: false,
        } };
        const byItem = searchViewer({
            query: "inv/0042", expandedLines: ["section-a"], childLines,
        });
        expect(visibleIds(byItem)).toEqual(["section-a", "account-1", "aml-8"]);
        const byAccount = searchViewer({
            query: "1000", expandedLines: ["section-a"], childLines,
        });
        expect(visibleIds(byAccount)).toEqual(["section-a", "account-1"]);
        const noSearch = searchViewer({
            query: "", expandedLines: ["section-a"], childLines,
        });
        expect(visibleIds(noSearch)).toEqual(
            ["section-a", "account-1", "account-2", "account-9"],
        );
    });

    test("showing count ignores loading rows and fetched items", () => {
        const viewer = searchViewer({
            query: "10",
            expandedLines: ["section-a", "account-1"],
            childLines: { "account-1": {
                lines: [{ id: "aml-8", name: "Opening float" }],
                hasMore: true, nextOffset: 1, totalCount: 5, loading: false,
            } },
        });
        // A stale window from the previous render must not leak into X.
        viewer._window = { total: 99 };
        expect(visibleIds(viewer)).toEqual([
            "section-a", "account-1", "aml-8", "loadmore-account-1", "account-2",
        ]);
        expect(viewer.visibleCount).toBe(3);
    });
});

describe("eh dynamic report - control capabilities", () => {
    test("comparison appears only on handlers that compute it", () => {
        for (const code of [
            "executive_summary", "trial_balance", "profit_and_loss",
            "balance_sheet",
        ]) {
            expect(reportCapabilitiesForCode(code)).toInclude("comparison");
        }
        for (const code of [
            "general_ledger", "partner_ledger",
            "aged_receivable", "aged_payable", "cash_flow",
            "customer_statement", "vendor_statement", "analytic_balance",
            "deferred_revenue", "deferred_expense", "bank_reconciliation",
        ]) {
            expect(reportCapabilitiesForCode(code)).not.toInclude("comparison");
        }
    });

    test("account types hide where dimension cannot preserve report meaning", () => {
        for (const code of [
            "trial_balance", "profit_and_loss", "balance_sheet",
            "general_ledger", "partner_ledger", "aged_receivable",
            "aged_payable", "cash_flow", "customer_statement",
            "vendor_statement", "analytic_balance",
        ]) {
            expect(reportCapabilitiesForCode(code)).toInclude("account_types");
        }
        for (const code of [
            "deferred_revenue", "deferred_expense", "bank_reconciliation",
        ]) {
            expect(reportCapabilitiesForCode(code)).not.toInclude("account_types");
        }
    });

    test("unknown report fails closed with no optional controls", () => {
        expect(reportCapabilitiesForCode("unknown_report")).toEqual([]);
    });

    test("period and analytic axes appear only on implemented reports", () => {
        for (const code of [
            "executive_summary", "trial_balance", "profit_and_loss",
            "balance_sheet",
        ]) {
            expect(reportCapabilitiesForCode(code)).toInclude("nperiod");
        }
        for (const code of [
            "trial_balance", "profit_and_loss", "balance_sheet",
        ]) {
            expect(reportCapabilitiesForCode(code)).toInclude("analytic_columns");
        }
        for (const code of ["executive_summary"]) {
            expect(reportCapabilitiesForCode(code)).not.toInclude(
                "analytic_columns",
            );
        }
    });

    test("bank proof hides dimensions it cannot apply symmetrically", () => {
        expect(unsupportedOptionsForCode("bank_reconciliation")).toEqual([
            "partner_ids", "account_ids", "account_type_ids",
            "analytic_account_ids", "analytic_plan_ids",
            "presentation_currency_id", "show_zero",
        ]);
        expect(unsupportedOptionsForCode("profit_and_loss")).toEqual([]);
        for (const code of ["deferred_revenue", "deferred_expense"]) {
            expect(unsupportedOptionsForCode(code)).toEqual([
                "journal_ids", "partner_ids", "account_ids",
                "account_type_ids", "analytic_account_ids",
                "analytic_plan_ids", "show_zero",
            ]);
        }
    });
});

describe("eh dynamic report - scoped cell drilldown", () => {
    const options = {
        date: { date_from: "2026-01-01", date_to: "2026-12-31" },
        company_ids: [10, 20],
        primary_company_id: 10,
    };

    test("derived cells use authoritative displayed accounting scope", () => {
        const prior = drilldownOptionsForColumn(
            options,
            { expression_label: "prior_amount", scope: {
                date_from: "2025-01-01", date_to: "2025-12-31",
                comparison_index: 1, is_total: false,
            } },
        );
        expect(prior.date).toEqual({
            date_from: "2025-01-01", date_to: "2025-12-31",
        });
        const balanceSheetPrior = drilldownOptionsForColumn(
            options,
            { expression_label: "prior_amount", scope: {
                date_from: "0001-01-01", date_to: "2025-12-31",
            } },
        );
        expect(balanceSheetPrior.date).toEqual({
            date_from: "0001-01-01", date_to: "2025-12-31",
        });
        const period = drilldownOptionsForColumn(
            options,
            { expression_label: "period_2", scope: {
                date_from: "2024-01-01", date_to: "2024-12-31",
                comparison_index: 2,
            } },
        );
        expect(period.date).toEqual({
            date_from: "2024-01-01", date_to: "2024-12-31",
        });
    });

    test("company pivot cell narrows to displayed company", () => {
        const scoped = drilldownOptionsForColumn(
            options,
            { expression_label: "company_20", scope: {
                company_ids: [20],
            } },
        );
        expect(scoped.company_ids).toEqual([20]);
        expect(scoped.primary_company_id).toBe(20);
    });

    test("analytic axis intersects public row filters through private keys", () => {
        const scoped = drilldownOptionsForColumn(
            {
                ...options,
                analytic_account_ids: [30], analytic_plan_ids: [40],
            },
            { expression_label: "analytic_50", scope: {
                date_from: "2026-01-01", date_to: "2026-12-31",
                analytic_account_ids: [50], analytic_plan_ids: [],
                is_total: false,
            } },
        );
        expect(scoped.analytic_account_ids).toEqual([30]);
        expect(scoped.analytic_plan_ids).toEqual([40]);
        expect(scoped._eh_analytic_column_account_ids).toEqual([50]);
        expect(scoped._eh_analytic_column_plan_ids).toEqual([]);
        expect(scoped._eh_analytic_column_is_total).toBe(false);

        const total = drilldownOptionsForColumn(
            options,
            { expression_label: "total", scope: {
                analytic_account_ids: [], analytic_plan_ids: [], is_total: true,
            } },
        );
        expect(total._eh_analytic_column_account_ids).toEqual([]);
        expect(total._eh_analytic_column_plan_ids).toEqual([]);
        expect(total._eh_analytic_column_is_total).toBe(true);
    });

    test("legacy comparison and company columns retain truthful drilldown", () => {
        const meta = {
            prior_date_from: "2025-01-01",
            prior_date_to: "2025-12-31",
            comparison_periods: [
                { from: "2025-01-01", to: "2025-12-31" },
                { from: "2024-01-01", to: "2024-12-31" },
            ],
            company_ids: [10, 20],
        };
        expect(drilldownOptionsForColumn(
            options, { expression_label: "prior_amount" }, meta,
        ).date).toEqual({
            date_from: "2025-01-01", date_to: "2025-12-31",
        });
        expect(drilldownOptionsForColumn(
            options, { expression_label: "prior_2" }, meta,
        ).date).toEqual({
            date_from: "2024-01-01", date_to: "2024-12-31",
        });
        expect(drilldownOptionsForColumn(
            options, { expression_label: "period_1" }, meta,
        ).date).toEqual({
            date_from: "2025-01-01", date_to: "2025-12-31",
        });
        const company = drilldownOptionsForColumn(
            options, { expression_label: "group_2" }, meta,
        );
        expect(company.company_ids).toEqual([20]);
        expect(company.primary_company_id).toBe(20);
    });

    test("legacy unique period and TB labels decode report metadata", () => {
        const meta = {
            comparison_order: "descending",
            comparison_periods: [
                { from: "2025-01-01", to: "2025-12-31" },
                { from: "2024-01-01", to: "2024-12-31" },
            ],
        };
        const current = drilldownOptionsForColumn(options, {
            expression_label: "amount__period_current__period_debit",
        }, meta);
        expect(current.date).toEqual(options.date);
        expect(current._eh_column_expression)
            .toBe("amount__period_current__period_debit");
        const comparison = drilldownOptionsForColumn(options, {
            expression_label:
                "amount__period_comparison_2__opening_debit",
        }, meta);
        expect(comparison.date).toEqual({
            date_from: "2024-01-01", date_to: "2024-12-31",
        });

        const ascending = drilldownOptionsForColumn(options, {
            expression_label:
                "amount__period_comparison_1__closing_credit",
        }, {
            ...meta,
            comparison_order: "ascending",
            comparison_periods: [...meta.comparison_periods].reverse(),
        });
        expect(ascending.date).toEqual({
            date_from: "2025-01-01", date_to: "2025-12-31",
        });
    });

    test("legacy snapshot columns remain cumulative", () => {
        const current = drilldownOptionsForColumn(
            options, { expression_label: "amount__period_current" },
            { date_basis: "as_of" },
        );
        expect(current.date).toEqual({
            date_from: "0001-01-01", date_to: "2026-12-31",
        });
        const prior = drilldownOptionsForColumn(
            options, { expression_label: "prior_amount" },
            { date_basis: "as_of", prior_date_to: "2025-12-31" },
        );
        expect(prior.date).toEqual({
            date_from: "0001-01-01", date_to: "2025-12-31",
        });
    });

    test("unverifiable legacy or malformed scoped columns fail closed", () => {
        for (const column of [
            { expression_label: "prior_2" },
            { expression_label: "group_2" },
            { expression_label: "amount__analytic_plan_4" },
            { expression_label: "amount__unknown_axis" },
            { expression_label: "period_2", scope: {
                date_from: "2026-12-31", date_to: "2026-01-01",
            } },
            { expression_label: "analytic_3", scope: {
                analytic_account_ids: [0],
            } },
            { expression_label: "period_3", scope: { unexpected: true } },
        ]) {
            expect(drilldownOptionsForColumn(options, column, {})).toBe(null);
        }
    });

    test("variance and budget cells are not falsely drillable", () => {
        for (const expression_label of [
            "variance", "variance_pct", "budget", "budget_variance",
        ]) {
            expect(drilldownOptionsForColumn(
                options, { expression_label }, {},
            )).toBe(null);
        }
        expect(drilldownOptionsForColumn(
            options,
            { expression_label: "variance__period_current", scope: {
                date_from: "2026-01-01", date_to: "2026-12-31",
            } },
            {},
        )).toBe(null);
    });

    test("only rows backed by handler actions look clickable", () => {
        expect(isDrillableReportLine(
            "trial_balance", { id: "account-42" },
        )).toBe(true);
        expect(isDrillableReportLine(
            "trial_balance", { id: "account-unaffected-earnings" },
        )).toBe(false);
        expect(isDrillableReportLine(
            "aged_receivable", { id: "partner-none" },
        )).toBe(true);
        expect(isDrillableReportLine(
            "analytic_balance", { id: "analytic-42" },
        )).toBe(false);
        expect(isDrillableReportLine(
            "cash_flow", { id: "operating-expenses" },
        )).toBe(false);
        expect(isDrillableReportLine(
            "executive_summary", { id: "exec-cash" },
        )).toBe(true);
        expect(isDrillableReportLine(
            "executive_summary", { id: "exec-gross-margin" },
        )).toBe(false);
    });

    test("viewer uses immutable action report code for drilldowns", () => {
        const viewer = {
            reportCode: "profit_and_loss",
            state: {},
        };
        expect(EhDynamicReportViewer.prototype.isDrillableLine.call(
            viewer, { id: "account-42" },
        )).toBe(true);
    });

    test("global analytic filters hide inexact native drilldown", () => {
        const ordinary = { expression_label: "amount" };
        const allocated = {
            expression_label: "amount__analytic_51",
            scope: {
                date_from: "2026-01-01", date_to: "2026-12-31",
                analytic_account_ids: [51], analytic_plan_ids: [],
                comparison_index: 0, is_total: false,
            },
        };
        const line = { id: "account-4000", columns: [{ value: 10 }] };
        let nativeCalls = 0;
        let weightedCalls = 0;
        let weightedAmount = null;
        const viewer = {
            state: {
                options: {
                    ...options, analytic_account_ids: [41], cash_basis: false,
                },
                payload: { meta: {} },
            },
            isDrillableLine: () => true,
            valueColumnDefs: () => [ordinary],
            hasGlobalAnalyticRowFilters:
                EhDynamicReportViewer.prototype.hasGlobalAnalyticRowFilters,
            onLineClick() { nativeCalls += 1; },
            onOpenAnalyticDrilldown(_line, _column, amount) {
                weightedCalls += 1;
                weightedAmount = amount;
            },
        };
        expect(EhDynamicReportViewer.prototype.isDrillableColumn.call(
            viewer, line, ordinary,
        )).toBe(false);
        EhDynamicReportViewer.prototype.onAmountCellClick.call(
            viewer, line, ordinary,
        );
        expect(nativeCalls).toBe(0);
        expect(weightedCalls).toBe(0);

        viewer.valueColumnDefs = () => [allocated];
        expect(EhDynamicReportViewer.prototype.isDrillableColumn.call(
            viewer, line, allocated,
        )).toBe(true);
        EhDynamicReportViewer.prototype.onAmountCellClick.call(
            viewer, line, allocated, 0,
        );
        expect(nativeCalls).toBe(0);
        expect(weightedCalls).toBe(1);
        expect(weightedAmount).toBe(10);
    });

    test("drilldown value comes from row cell, not column definition", () => {
        const definitions = [
            { expression_label: "amount" },
            { expression_label: "prior_amount" },
        ];
        const line = { columns: [
            { expression_label: "amount", value: 0 },
            { expression_label: "prior_amount", value: 77 },
        ] };
        expect(lineCellHasValue(line, definitions[0], definitions)).toBe(true);
        expect(lineCellHasValue(line, definitions[1], definitions)).toBe(true);
        expect(lineCellHasValue(
            { columns: [{ value: null }] }, definitions[0], definitions,
        )).toBe(false);
        expect(lineCellHasValue(
            { columns: [{ value: "" }] }, definitions[0], definitions,
        )).toBe(false);
    });
});

describe("eh dynamic report - weighted analytic cell detail", () => {
    const pageToken = "a".repeat(64);
    const columns = [
        { key: "date", name: "Date", figure_type: "date" },
        { key: "move", name: "Move", figure_type: "string" },
        { key: "partner", name: "Partner", figure_type: "string" },
        { key: "label", name: "Label", figure_type: "string" },
        {
            key: "allocated_amount", name: "Allocated Amount",
            figure_type: "monetary",
        },
    ];
    const currency = {
        id: 1, name: "AUD", symbol: "$", position: "before",
        decimal_places: 2,
    };
    const scope = {
        date_from: "2026-01-01", date_to: "2026-12-31",
        analytic_account_ids: [51], analytic_plan_ids: [],
        comparison_index: 0, is_total: false,
    };
    const row = {
        id: "aml-101", move_line_id: 101, move_id: 201,
        values: {
            date: "2026-03-04", move: "MISC/2026/0001",
            partner: "Example Pty Ltd", label: "Consulting",
            allocated_amount: 40,
        },
    };

    function page(overrides = {}) {
        return {
            columns, rows: [row], total: 40,
            offset: 0, limit: 80, total_count: 1, has_more: false,
            page_token: pageToken, currency, scope, ...overrides,
        };
    }

    test("only exact non-total analytic slices opt into weighted detail", () => {
        expect(allocatedAnalyticScopeForColumn({ scope })).toEqual(scope);
        expect(allocatedAnalyticScopeForColumn({ scope: {
            ...scope, analytic_account_ids: [], analytic_plan_ids: [61],
        } })).toEqual({
            ...scope, analytic_account_ids: [], analytic_plan_ids: [61],
        });
        for (const invalid of [
            {},
            { scope: { ...scope, is_total: true } },
            { scope: {
                ...scope, analytic_account_ids: [], analytic_plan_ids: [],
            } },
            { scope: { ...scope, analytic_account_ids: [0] } },
        ]) {
            expect(allocatedAnalyticScopeForColumn(invalid)).toBe(null);
        }
    });

    test("server page validator preserves an exact weighted ledger page", () => {
        expect(normalizeAnalyticDrilldownPage(page())).toEqual(page());
    });

    test("malformed weighted pages fail closed", () => {
        const malformed = [
            page({ columns: [...columns].reverse() }),
            page({ columns: columns.map((column, index) => (
                index === 4 ? { ...column, figure_type: "string" } : column
            )) }),
            page({ rows: [{
                ...row, values: { ...row.values, allocated_amount: NaN },
            }] }),
            page({ rows: [{
                ...row, values: { ...row.values, date: "04/03/2026" },
            }] }),
            page({ has_more: true }),
            page({ currency: { ...currency, position: "middle" } }),
            page({ scope: { ...scope, is_total: true } }),
            page({ scope: { ...scope, unexpected: true } }),
            page({ page_token: "not-a-digest" }),
        ];
        for (const candidate of malformed) {
            expect(normalizeAnalyticDrilldownPage(candidate)).toBe(null);
        }
    });

    test("viewer requests public options and appends a stable next page", async () => {
        patchTranslations();
        const calls = [];
        const secondRow = {
            ...row, id: "aml-102", move_line_id: 102, move_id: 202,
            values: {
                ...row.values, move: "MISC/2026/0002",
                allocated_amount: 60,
            },
        };
        const replies = [
            page({ total: 100, total_count: 2, has_more: true }),
            page({
                rows: [secondRow], total: 100, offset: 1,
                total_count: 2, has_more: false,
            }),
        ];
        const viewer = {
            _analyticDrilldownSequence: 0,
            state: {
                reportId: 7,
                payload: { execution_id: 901 },
                options: {
                    date: {
                        date_from: "2026-01-01", date_to: "2026-12-31",
                    },
                    company_ids: [3], analytic_account_ids: [41],
                    analytic_column_account_ids: [51], cash_basis: false,
                    _eh_column_expression: "untrusted",
                    _eh_analytic_column_account_ids: [999],
                },
                analyticDrilldown: null,
            },
            orm: { async call(...args) {
                calls.push(args);
                return replies[calls.length - 1];
            } },
            _loadAnalyticDrilldownPage:
                EhDynamicReportViewer.prototype._loadAnalyticDrilldownPage,
        };
        await EhDynamicReportViewer.prototype.onOpenAnalyticDrilldown.call(
            viewer,
            { id: "account-4000", name: "Sales" },
            {
                name: "Consulting", expression_label: "amount__p0__aa51",
                scope,
            },
            100,
        );
        expect(calls[0][0]).toBe("eh.account.dynamic.report");
        expect(calls[0][1]).toBe("get_analytic_column_drilldown_page");
        expect(calls[0][2][0]).toEqual([7]);
        expect(calls[0][2][2]).toBe("account-4000");
        expect(calls[0][2][3]).toBe("amount__p0__aa51");
        expect(calls[0][2][4]).toBe(0);
        expect(calls[0][2][5]).toBe(80);
        expect(calls[0][2][6]).toBe(901);
        expect(calls[0][2][7]).toBe(100);
        expect(calls[0][2][8]).toBe(null);
        expect(calls[0][2][1].analytic_account_ids).toEqual([41]);
        expect(calls[0][2][1].analytic_column_account_ids).toEqual([51]);
        expect(calls[0][2][1]._eh_column_expression).toBe(undefined);
        expect(calls[0][2][1]._eh_analytic_column_account_ids)
            .toBe(undefined);
        expect(viewer.state.analyticDrilldown.rows.length).toBe(1);
        expect(viewer.state.analyticDrilldown.hasMore).toBe(true);

        await EhDynamicReportViewer.prototype
            .onLoadMoreAnalyticDrilldown.call(viewer);
        expect(calls[1][2][4]).toBe(1);
        expect(calls[1][2][8]).toBe(pageToken);
        expect(viewer.state.analyticDrilldown.rows.length).toBe(2);
        expect(viewer.state.analyticDrilldown.totalCount).toBe(2);
        expect(viewer.state.analyticDrilldown.hasMore).toBe(false);
    });

    test("equal-count and equal-total candidate drift fails token binding", async () => {
        patchTranslations();
        let callCount = 0;
        const viewer = {
            _analyticDrilldownSequence: 0,
            state: {
                reportId: 7,
                payload: { execution_id: 902 },
                options: {
                    date: {
                        date_from: "2026-01-01", date_to: "2026-12-31",
                    },
                    company_ids: [3], analytic_column_account_ids: [51],
                    cash_basis: false,
                },
                analyticDrilldown: null,
            },
            orm: { async call() {
                callCount += 1;
                if (callCount === 1) {
                    return page({
                        total: 100, total_count: 2, has_more: true,
                    });
                }
                return page({
                    rows: [{
                        ...row, id: "aml-999", move_line_id: 999,
                    }],
                    total: 100, offset: 1, total_count: 2,
                    has_more: false, page_token: "b".repeat(64),
                });
            } },
            _loadAnalyticDrilldownPage:
                EhDynamicReportViewer.prototype._loadAnalyticDrilldownPage,
        };
        await EhDynamicReportViewer.prototype.onOpenAnalyticDrilldown.call(
            viewer,
            { id: "account-4000", name: "Sales" },
            { expression_label: "amount__aa51", scope },
            100,
        );
        await EhDynamicReportViewer.prototype
            .onLoadMoreAnalyticDrilldown.call(viewer);
        expect(viewer.state.analyticDrilldown.rows.length).toBe(1);
        expect(viewer.state.analyticDrilldown.pageToken).toBe(pageToken);
        expect(Boolean(viewer.state.analyticDrilldown.error)).toBe(true);
    });

    test("missing execution or displayed amount never calls the endpoint", async () => {
        let rpcCount = 0;
        const viewer = {
            _analyticDrilldownSequence: 0,
            state: {
                reportId: 7, payload: null,
                options: { cash_basis: false }, analyticDrilldown: null,
            },
            orm: { async call() { rpcCount += 1; } },
            _loadAnalyticDrilldownPage:
                EhDynamicReportViewer.prototype._loadAnalyticDrilldownPage,
        };
        const line = { id: "account-4000", name: "Sales" };
        const column = { expression_label: "amount__aa51", scope };
        await EhDynamicReportViewer.prototype.onOpenAnalyticDrilldown.call(
            viewer, line, column, 40,
        );
        viewer.state.payload = { execution_id: 903 };
        await EhDynamicReportViewer.prototype.onOpenAnalyticDrilldown.call(
            viewer, line, column, undefined,
        );
        expect(rpcCount).toBe(0);
        expect(viewer.state.analyticDrilldown).toBe(null);
    });

    test("cash-basis analytic cells never call weighted detail", async () => {
        let rpcCount = 0;
        const viewer = {
            _analyticDrilldownSequence: 0,
            state: {
                reportId: 7, options: { cash_basis: true },
                payload: { execution_id: 901 },
                analyticDrilldown: null,
            },
            orm: { async call() { rpcCount += 1; } },
            _loadAnalyticDrilldownPage:
                EhDynamicReportViewer.prototype._loadAnalyticDrilldownPage,
        };
        await EhDynamicReportViewer.prototype.onOpenAnalyticDrilldown.call(
            viewer,
            { id: "account-4000", name: "Sales" },
            { expression_label: "amount__aa51", scope },
            40,
        );
        expect(rpcCount).toBe(0);
        expect(viewer.state.analyticDrilldown).toBe(null);
    });

    test("RPC failure stays inside the modal and never opens another action", async () => {
        patchTranslations();
        const viewer = {
            _analyticDrilldownSequence: 0,
            state: {
                reportId: 7, options: { cash_basis: false },
                payload: { execution_id: 901 },
                analyticDrilldown: null,
            },
            orm: { async call() { throw new Error("Access denied"); } },
            _loadAnalyticDrilldownPage:
                EhDynamicReportViewer.prototype._loadAnalyticDrilldownPage,
        };
        await EhDynamicReportViewer.prototype.onOpenAnalyticDrilldown.call(
            viewer,
            { id: "account-4000", name: "Sales" },
            { expression_label: "amount__aa51", scope },
            40,
        );
        expect(viewer.state.analyticDrilldown.error).toBe("Access denied");
        expect(viewer.state.analyticDrilldown.rows).toEqual([]);
        expect(viewer.state.analyticDrilldown.loading).toBe(false);
    });
});

describe("eh dynamic report - grouped column headers", () => {
    const columns = [
        { name: "Account" }, { name: "Current A" },
        { name: "Current B" }, { name: "Prior A" },
        { name: "Prior B" },
    ];

    test("valid spans cover flat authoritative columns exactly", () => {
        const rows = normalizeColumnHeaderRows(columns, [
            [
                { name: "Account", rowspan: 2 },
                { name: "Current", colspan: 2 },
                { name: "Prior", colspan: 2 },
            ],
            [
                { name: "A" }, { name: "B" },
                { name: "A" }, { name: "B" },
            ],
        ]);
        expect(rows.length).toBe(2);
        expect(rows[0][0]).toEqual({
            name: "Account", colspan: 1, rowspan: 2,
            start: 0, key: "0-0-1-2",
        });
        expect(rows[0][1].colspan).toBe(2);
    });

    test("malformed spans return null for flat-header fallback", () => {
        for (const rows of [
            [[{ name: "Overflow", colspan: 6 }]],
            [[{ name: "Boolean", colspan: true }]],
            [
                [{ name: "Account", rowspan: 2 }, { name: "All", colspan: 4 }],
                [{ name: "Hole" }],
            ],
        ]) {
            expect(normalizeColumnHeaderRows(columns, rows)).toBe(null);
        }
        expect(normalizeColumnHeaderRows(columns, undefined)).toBe(null);
    });
});

describe("eh dynamic report - custom builder drilldown", () => {
    test("only account aggregates are actionable", () => {
        expect(isDrillableReportLine("custom_report", {
            id: "line-42", meta: { kind: "account_aggregate" },
        })).toBe(true);
        expect(isDrillableReportLine("custom_report", {
            id: "line-43", meta: { kind: "computed" },
        })).toBe(false);
        expect(isDrillableReportLine("custom_report", {
            id: "section-42-header", meta: { kind: "section_header" },
        })).toBe(false);
    });
});

describe("eh dynamic report - translated filter chips", () => {
    test("interpolates comparison, analytic, and aging values", () => {
        patchTranslations();
        const viewer = {
            state: {
                options: {
                    comparison: "custom",
                    comparison_custom_date_from: "2025-07-01",
                    comparison_custom_date_to: "2026-06-30",
                    comparison_number: 2,
                    comparison_order: "descending",
                    journal_ids: [11],
                    partner_ids: [],
                    account_ids: [],
                    account_type_ids: ["income"],
                    analytic_plan_ids: [],
                    analytic_account_ids: [],
                    analytic_column_plan_ids: [],
                    analytic_column_account_ids: [],
                    horizontal_group_by: null,
                    cash_flow_method: "direct",
                    cash_flow_reconciled: false,
                    reconcile_state: "all",
                    aging_basis: "invoice",
                    aging_interval: 45,
                    aging_bucket_count: 6,
                },
                choices: {
                    journals: [{ id: 11, code: "BNK", name: "Main" }],
                    partners: [],
                    accounts: [],
                    analyticPlans: [],
                    analyticAccounts: [],
                },
            },
            supportsComparison: () => true,
            supportsOption: () => true,
            supportsAccountTypes: () => true,
            supportsAnalyticColumns: () => true,
            isCashFlow: false,
            isAged: true,
        };

        const labels = EhDynamicReportViewer.prototype.activeChips
            .call(viewer).map((chip) => String(chip.label));

        expect(labels).toInclude("Compare: 2025-07-01 to 2026-06-30");
        expect(labels).toInclude("Comparison periods: 2");
        expect(labels).toInclude("Journal: BNK Main");
        expect(labels).toInclude("Type: Income");
        expect(labels).toInclude("Interval: 45d");
        expect(labels).toInclude("Buckets: 6");
        expect(labels.some((label) => label.includes("%s"))).toBe(false);
    });
});

describe("eh dynamic report: a failed server call shows its own message", () => {
    const fiscalText = "Trial Balance period 2026-01-01 to 2026-10-27 "
        + "crosses a fiscal-year boundary for My Company.";

    test("a server error shows the exception text, not the generic title", () => {
        const rpcError = new Error("Odoo Server Error");
        rpcError.data = {
            name: "odoo.exceptions.UserError",
            message: fiscalText,
        };
        expect(errorMessage(rpcError)).toBe(fiscalText);
    });

    test("a client error or a blank server text keeps the error message", () => {
        expect(errorMessage(new Error("Connection lost"))).toBe("Connection lost");
        const blank = new Error("Odoo Server Error");
        blank.data = { message: "  " };
        expect(errorMessage(blank)).toBe("Odoo Server Error");
        expect(errorMessage("plain text")).toBe("plain text");
        expect(errorMessage(null)).toBe("null");
    });

    test("a redirect warning shows its message argument only", () => {
        const redirect = new Error("Odoo Server Error");
        redirect.data = {
            name: "odoo.exceptions.RedirectWarning",
            message: fiscalText + "\n42\nOpen settings",
            arguments: [fiscalText, 42, "Open settings"],
        };
        expect(errorMessage(redirect)).toBe(fiscalText);
    });

    test("a subclass that Odoo flags as a user error shows its text", () => {
        const subclass = new Error("Odoo Server Error");
        subclass.data = {
            name: "odoo.addons.some_module.models.SomeError",
            message: fiscalText,
            context: { exception_class: "odoo.exceptions.UserError" },
        };
        expect(errorMessage(subclass)).toBe(fiscalText);
    });

    test("an unexpected server exception keeps the generic title with its class", () => {
        const crash = new Error("Odoo Server Error");
        crash.data = { name: "builtins.KeyError", message: "'account_code'" };
        expect(errorMessage(crash)).toBe("Odoo Server Error (KeyError)");
        const sql = new Error("Odoo Server Error");
        sql.data = {
            name: "psycopg2.errors.UndefinedColumn",
            message: 'column "x" does not exist\nLINE 1: SELECT x FROM account_move',
        };
        expect(errorMessage(sql)).toBe("Odoo Server Error (UndefinedColumn)");
    });

    test("the report banner of a failed render shows the server text", async () => {
        const viewer = {
            reportCode: null,
            _refreshSequence: 0,
            _childLinesSequence: 0,
            state: {
                reportId: 7,
                options: {},
                loading: false,
                error: null,
                payload: null,
                childLines: {},
                scrollTop: 0,
                tableQuery: "",
                expandedLines: [],
            },
            bodyRef: null,
            normalizeCapabilityOptions() {},
            onCloseAnalyticDrilldown() {},
            orm: {
                async call() {
                    const rpcError = new Error("Odoo Server Error");
                    rpcError.data = {
                        name: "odoo.exceptions.UserError",
                        message: fiscalText,
                    };
                    throw rpcError;
                },
            },
        };
        await EhDynamicReportViewer.prototype.refresh.call(viewer);
        expect(viewer.state.error).toBe(fiscalText);
        expect(viewer.state.loading).toBe(false);
    });
});

describe("eh dynamic report: expand and collapse all are remembered", () => {
    function foldViewer(calls) {
        return {
            reportCode: "trial_balance",
            state: {
                payload: {
                    lines: [
                        { id: "g1", unfoldable: true },
                        { id: "g2", unfoldable: true },
                        { id: "account-1", unfoldable: true, lazy: true },
                        { id: "account-2", unfoldable: false },
                    ],
                },
                expandedLines: ["g2"],
            },
            orm: {
                call(model, method, args) {
                    calls.push([model, method, args]);
                    return Promise.resolve(true);
                },
            },
            _persistFold: EhDynamicReportViewer.prototype._persistFold,
            isExpanded: EhDynamicReportViewer.prototype.isExpanded,
        };
    }

    test("expand all opens every group and clears the saved folds", async () => {
        const calls = [];
        const viewer = foldViewer(calls);
        EhDynamicReportViewer.prototype.onExpandAll.call(viewer);
        await viewer._foldSave;
        expect(viewer.state.expandedLines).toEqual(["g1", "g2"]);
        expect(calls).toEqual([
            ["eh.account.report.fold.state", "reset_for_user", ["trial_balance"]],
        ]);
    });

    test("collapse all folds every group and saves the folds", async () => {
        const calls = [];
        const viewer = foldViewer(calls);
        EhDynamicReportViewer.prototype.onCollapseAll.call(viewer);
        await viewer._foldSave;
        expect(viewer.state.expandedLines).toEqual([]);
        expect(calls).toEqual([
            [
                "eh.account.report.fold.state",
                "set_many_for_user",
                ["trial_balance", { __default__: false, g1: false, g2: false }],
            ],
        ]);
    });

    test("fold saves reach the server in click order", async () => {
        const calls = [];
        const viewer = foldViewer(calls);
        const proto = EhDynamicReportViewer.prototype;
        proto.onCollapseAll.call(viewer);
        proto.onToggleLine.call(viewer, { id: "g1", unfoldable: true });
        proto.onExpandAll.call(viewer);
        await viewer._foldSave;
        expect(calls.map((call) => call[1])).toEqual([
            "set_many_for_user", "set_for_user", "reset_for_user",
        ]);
    });

    test("a group without its own saved fold follows collapse all", async () => {
        const viewer = {
            reportCode: "trial_balance",
            _refreshSequence: 0,
            _childLinesSequence: 0,
            state: {
                reportId: 7,
                options: {},
                loading: false,
                error: null,
                payload: null,
                childLines: {},
                scrollTop: 0,
                tableQuery: "",
                expandedLines: [],
            },
            bodyRef: null,
            normalizeCapabilityOptions() {},
            onCloseAnalyticDrilldown() {},
            orm: {
                async call(model, method) {
                    if (method === "get_for_user") {
                        return { __default__: false, g1: true };
                    }
                    return {
                        lines: [
                            { id: "g1", unfoldable: true },
                            { id: "g2", unfoldable: true },
                        ],
                    };
                },
            },
        };
        await EhDynamicReportViewer.prototype.refresh.call(viewer);
        expect(viewer.state.expandedLines).toEqual(["g1"]);
    });
});
