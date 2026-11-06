/** @odoo-module **/

import { describe, expect, test } from "@odoo/hoot";
import { EhDynamicReportViewer } from "@eh_account_dynamic_reports/components/dynamic_report/dynamic_report";
import {
    BUDGET_VARIANCE_EXPRESSIONS,
    reconcileBudgetSelection,
} from "../src/budget_filter";

describe("dynamic budget report filter", () => {
    test("company scope clears a stale selected budget", () => {
        const choices = [{ id: 10 }, { id: 12 }];
        expect(reconcileBudgetSelection("10", choices)).toBe(10);
        expect(reconcileBudgetSelection(11, choices)).toBe(false);
        expect(reconcileBudgetSelection("invalid", choices)).toBe(false);
    });

    test("budget amount and percentage variances use comparison colours", () => {
        expect(BUDGET_VARIANCE_EXPRESSIONS.has("budget_variance")).toBe(true);
        expect(BUDGET_VARIANCE_EXPRESSIONS.has("budget_variance_pct")).toBe(true);
        expect(BUDGET_VARIANCE_EXPRESSIONS.has("budget")).toBe(false);
    });

    test("legacy saved view clears active budget extension options", async () => {
        let refreshed = 0;
        const viewer = {
            state: {
                options: {
                    company_ids: [3],
                    budget_id: 12,
                    budget_apportionment: "period_exact",
                    budget_state_filter: "all",
                },
                currentSavedViewId: null,
            },
            orm: { async call() { return { company_ids: [7] }; } },
            savedViewOptionDefaults:
                EhDynamicReportViewer.prototype.savedViewOptionDefaults,
            refresh() { refreshed += 1; },
            notification: { add() {} },
        };
        await EhDynamicReportViewer.prototype.onSavedViewChange.call(
            viewer, { target: { value: "19" } },
        );
        expect(viewer.state.options.company_ids).toEqual([7]);
        expect(viewer.state.options.budget_id).toBe(false);
        expect(viewer.state.options.budget_apportionment).toBe("daily");
        expect(viewer.state.options.budget_state_filter).toBe("live");
        expect(viewer.state.currentSavedViewId).toBe(19);
        expect(refreshed).toBe(1);
    });

    test("budget wrapper forwards analytic cell value index", () => {
        const scope = {
            date_from: "2026-01-01",
            date_to: "2026-12-31",
            analytic_account_ids: [51],
            analytic_plan_ids: [],
            comparison_index: 0,
            is_total: false,
        };
        const line = {
            id: "account-4000",
            columns: [{ value: 10 }, { value: 20 }, { value: 47.5 }],
        };
        const column = {
            expression_label: "amount__p0__aa51",
            scope,
        };
        let openedAmount = null;
        const viewer = {
            state: {
                options: { cash_basis: false },
                payload: { execution_id: 81 },
            },
            isDrillableLine: () => true,
            onOpenAnalyticDrilldown(_line, _column, amount) {
                openedAmount = amount;
            },
        };
        EhDynamicReportViewer.prototype.onAmountCellClick.call(
            viewer, line, column, 2,
        );
        expect(openedAmount).toBe(47.5);
    });
});
