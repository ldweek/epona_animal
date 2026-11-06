/** @odoo-module **/
// ============================================================================
// ERP Heritage
// Copyright (C) 2026 (https://www.erpheritage.com.au/)
// ============================================================================

import { describe, expect, test } from "@odoo/hoot";
import {
    cookieReportTheme,
    loadReportTheme,
    normalizeReportTheme,
    reportThemeDatabase,
    reportThemeStorageKey,
    resolveInitialReportTheme,
    saveReportTheme,
} from "@eh_account_dynamic_reports/components/dynamic_report/report_theme";

describe("eh dynamic report - local light/dark preference", () => {
    test("accepts only explicit light and dark values", () => {
        expect(normalizeReportTheme("light")).toBe("light");
        expect(normalizeReportTheme("dark")).toBe("dark");
        expect(normalizeReportTheme("system")).toBe(null);
        expect(normalizeReportTheme(false)).toBe(null);
    });

    test("scopes storage key to current user with anonymous fallback", () => {
        expect(reportThemeStorageKey(42, "demo-a")).toBe(
            "eh_account_dynamic_reports.theme.v1.demo-a.42",
        );
        expect(reportThemeStorageKey(null)).toBe(
            "eh_account_dynamic_reports.theme.v1.default.anonymous",
        );
    });

    test("database scope prevents same UID leaking across databases", () => {
        expect(reportThemeDatabase({ odoo: { info: { db: "company a" } } }))
            .toBe("company a");
        expect(reportThemeDatabase({ location: { search: "?db=company%20b" } }))
            .toBe("company b");
        expect(reportThemeStorageKey(9, "company a")).not.toBe(
            reportThemeStorageKey(9, "company b"),
        );
    });

    test("stored preference wins, then Odoo cookie, then system preference", () => {
        expect(resolveInitialReportTheme({
            storedTheme: "light",
            cookieValue: "color_scheme=dark",
            prefersDark: true,
        })).toBe("light");
        expect(resolveInitialReportTheme({
            storedTheme: "invalid",
            cookieValue: "x=1; color_scheme=dark; y=2",
            prefersDark: false,
        })).toBe("dark");
        expect(resolveInitialReportTheme({ prefersDark: true })).toBe("dark");
        expect(resolveInitialReportTheme()).toBe("light");
    });

    test("cookie parser is strict and never accepts arbitrary values", () => {
        expect(cookieReportTheme("color_scheme=light")).toBe("light");
        expect(cookieReportTheme("x=dark; color_scheme=system")).toBe(null);
        expect(cookieReportTheme(null)).toBe(null);
    });

    test("loads and saves without any server dependency", () => {
        const values = new Map();
        const fakeWindow = {
            odoo: { info: { db: "theme-test" } },
            document: { cookie: "" },
            matchMedia: () => ({ matches: true }),
            localStorage: {
                getItem: (key) => values.get(key) || null,
                setItem: (key, value) => values.set(key, value),
            },
        };
        expect(loadReportTheme(7, "theme-test", fakeWindow)).toBe("dark");
        expect(saveReportTheme("light", 7, "theme-test", fakeWindow)).toBe(true);
        expect(loadReportTheme(7, "theme-test", fakeWindow)).toBe("light");
        expect(saveReportTheme("system", 7, "theme-test", fakeWindow)).toBe(false);
    });

    test("blocked storage/cookies/media never break report rendering", () => {
        const blockedWindow = {
            get document() { throw new Error("blocked"); },
            get localStorage() { throw new Error("blocked"); },
            matchMedia: () => { throw new Error("blocked"); },
        };
        expect(loadReportTheme(7, "theme-test", blockedWindow)).toBe("light");
        expect(saveReportTheme("dark", 7, "theme-test", blockedWindow)).toBe(false);
    });
});
