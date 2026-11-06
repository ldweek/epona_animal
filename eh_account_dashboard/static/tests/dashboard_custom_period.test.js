/** @odoo-module **/

import { describe, expect, test } from "@odoo/hoot";
import { patchTranslations } from "@web/../tests/web_test_helpers";

import {
    EhDashboard,
    customPeriodValidationError,
    shouldRunSilentRefresh,
} from "@eh_account_dashboard/dashboard/dashboard";

describe("financial dashboard custom period", () => {
    test("requires both date inputs", () => {
        expect(customPeriodValidationError("", "2026-08-25")).not.toBe(null);
        expect(customPeriodValidationError("2026-08-01", "")).not.toBe(null);
    });

    test("rejects a reversed range", () => {
        expect(
            customPeriodValidationError("2026-08-25", "2026-08-01"),
        ).not.toBe(null);
    });

    test("accepts an ordered inclusive range", () => {
        expect(
            customPeriodValidationError("2026-08-01", "2026-08-25"),
        ).toBe(null);
        expect(
            customPeriodValidationError("2026-08-25", "2026-08-25"),
        ).toBe(null);
    });
});

describe("financial dashboard auto refresh", () => {
    test("runs only in a visible tab with no request in flight", () => {
        expect(shouldRunSilentRefresh(false, false)).toBe(true);
        expect(shouldRunSilentRefresh(true, false)).toBe(false);
        expect(shouldRunSilentRefresh(false, true)).toBe(false);
        expect(shouldRunSilentRefresh(true, true)).toBe(false);
    });
});

describe("financial dashboard translated metrics", () => {
    test("interpolates values instead of exposing percent placeholders", () => {
        patchTranslations();
        const dashboard = Object.create(EhDashboard.prototype);
        dashboard.state = {
            snapshot: {
                currency: {
                    symbol: "$",
                    position: "before",
                    decimal_places: 2,
                },
            },
        };

        const labels = [
            dashboard.cashAccountSecondary(2),
            dashboard.receivableSecondary({
                receivable_overdue: 10,
                receivable_days_overdue_max: 4,
            }),
            dashboard.formatRatioValue({ value: 3, format: "days" }),
            dashboard.ratioDeltaLabel({ delta_pct: 12.5 }),
        ].map(String);

        expect(labels).toEqual([
            "2 cash account(s)",
            "Overdue $10.00 · oldest 4 days",
            "3.00 days",
            "+12.5% vs prior",
        ]);
        expect(labels.some((label) => label.includes("%s"))).toBe(false);
    });
});
