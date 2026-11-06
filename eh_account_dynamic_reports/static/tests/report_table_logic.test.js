/** @odoo-module **/
// ============================================================================
// ERP Heritage
// Copyright (C) 2026 (https://www.erpheritage.com.au/)
// ============================================================================
//
// WS5 hoot tests for the pure table-craft logic: virtual-scroll windowing,
// in-table search keep-set, and variance cell semantics. These cover the
// fragile maths in isolation (no rendered component needed).

import { describe, expect, test } from "@odoo/hoot";
import {
    ROW_HEIGHT,
    OVERSCAN,
    computeFilterKeepSet,
    sliceWindow,
    variantCellRole,
} from "@eh_account_dynamic_reports/components/dynamic_report/report_table_logic";

describe("eh dynamic report - virtual scroll window", () => {
    const makeLines = (n) =>
        Array.from({ length: n }, (_, i) => ({ id: "L" + i, name: "row " + i }));

    test("slice count is bounded by viewport + 2*overscan", () => {
        const lines = makeLines(5000);
        const viewportPx = 560; // 20 rows at ROW_HEIGHT=28
        const win = sliceWindow(lines, {
            rowHeight: ROW_HEIGHT,
            overscan: OVERSCAN,
            scrollTop: 28 * 1000, // deep into the list
            viewportPx,
        });
        const viewportRows = Math.ceil(viewportPx / ROW_HEIGHT);
        const maxRows = viewportRows + 2 * OVERSCAN;
        expect(win.lines.length).toBeLessThan(maxRows + 1);
        // A constant window regardless of the 5000-row payload.
        expect(win.total).toBe(5000);
        expect(win.lines.length).toBeLessThan(60);
    });

    test("spacers reproduce the full list height", () => {
        const lines = makeLines(1000);
        const win = sliceWindow(lines, {
            rowHeight: ROW_HEIGHT,
            overscan: OVERSCAN,
            scrollTop: 28 * 400,
            viewportPx: 560,
        });
        const renderedPx = win.lines.length * ROW_HEIGHT;
        // top + rendered + bottom == total height, so the scrollbar matches.
        expect(win.topPad + renderedPx + win.bottomPad).toBe(1000 * ROW_HEIGHT);
    });

    test("degenerate row height falls back to the full list", () => {
        const lines = makeLines(120);
        const win = sliceWindow(lines, {
            rowHeight: 0, // would divide by zero
            scrollTop: 100,
            viewportPx: 560,
        });
        expect(win.lines.length).toBe(120);
        expect(win.topPad).toBe(0);
        expect(win.bottomPad).toBe(0);
    });

    test("top of list renders from index 0", () => {
        const lines = makeLines(100);
        const win = sliceWindow(lines, {
            rowHeight: ROW_HEIGHT,
            overscan: OVERSCAN,
            scrollTop: 0,
            viewportPx: 280, // 10 rows
        });
        expect(win.startIndex).toBe(0);
        expect(win.topPad).toBe(0);
    });
});

describe("eh dynamic report - sticky header / windowed body separation", () => {
    // The column-header row is rendered exclusively from payload.columns in
    // <thead> (a position:sticky element pinned to the scroll container). The
    // windowed body slice must come ONLY from payload.lines, so a header can
    // never be injected mid-list during a virtual scroll. These pure-logic
    // tests lock that separation so a regression that leaks a header row into
    // the body slice fails here.

    const makeRows = (n) =>
        Array.from({ length: n }, (_, i) => ({
            id: "partner-" + i,
            name: "Seed Partner " + i,
            meta: { kind: "partner_aged" },
        }));

    test("windowed slice is a contiguous sub-array of the body lines", () => {
        const lines = makeRows(400);
        const win = sliceWindow(lines, {
            rowHeight: ROW_HEIGHT,
            overscan: OVERSCAN,
            scrollTop: 28 * 150, // mid-list, where the floating header was seen
            viewportPx: 560,
        });
        // Every windowed row is one of the body lines (by identity), and they
        // are consecutive starting at startIndex: no synthetic / header row is
        // spliced into the body.
        win.lines.forEach((row, i) => {
            expect(row).toBe(lines[win.startIndex + i]);
        });
    });

    test("no column-header row appears in the windowed body slice", () => {
        // Simulate a payload: the header comes from `columns` (thead), the
        // body from `lines`. The windowed slice must contain zero rows that
        // are a column-header (no expression_label-only / figure_type row),
        // i.e. the header is structurally never in the body window.
        const columns = [
            { expression_label: "partner", name: "PARTNER", figure_type: "string" },
            { expression_label: "not_due", name: "NOT DUE", figure_type: "monetary" },
            { expression_label: "total", name: "TOTAL", figure_type: "monetary" },
        ];
        const lines = makeRows(300);
        const win = sliceWindow(lines, {
            rowHeight: ROW_HEIGHT,
            overscan: OVERSCAN,
            scrollTop: 28 * 120,
            viewportPx: 560,
        });
        const columnNames = new Set(columns.map((c) => c.name));
        for (const row of win.lines) {
            // A body row has an id + meta.kind; a column header has neither.
            expect(typeof row.id).toBe("string");
            expect(columnNames.has(row.name)).toBe(false);
        }
    });
});

describe("eh dynamic report - in-table search keep-set", () => {
    // A tiny tree: Income section -> Sales account; Expenses -> Rent.
    const lines = [
        { id: "sec-income", name: "Income", parent_id: null },
        { id: "acc-sales", name: "4000 Sales", parent_id: "sec-income" },
        { id: "sec-exp", name: "Expenses", parent_id: null },
        { id: "acc-rent", name: "6000 Rent", parent_id: "sec-exp" },
    ];

    test("empty query returns null (no filter)", () => {
        expect(computeFilterKeepSet(lines, "")).toBe(null);
    });

    test("parent stays visible when only a collapsed child matches", () => {
        // Match the child account only; its parent section must be kept as
        // context so the matched row is never orphaned.
        const keep = computeFilterKeepSet(lines, "sales");
        expect(keep.has("acc-sales")).toBe(true);
        expect(keep.has("sec-income")).toBe(true);
        // Unrelated branch is dropped.
        expect(keep.has("acc-rent")).toBe(false);
        expect(keep.has("sec-exp")).toBe(false);
    });

    test("matching a section reveals its descendants", () => {
        const keep = computeFilterKeepSet(lines, "expenses");
        expect(keep.has("sec-exp")).toBe(true);
        expect(keep.has("acc-rent")).toBe(true);
    });

    test("no match yields an empty keep set", () => {
        const keep = computeFilterKeepSet(lines, "zzz-nothing");
        expect(keep.size).toBe(0);
    });
});

describe("eh dynamic report - variance cell semantics", () => {
    test("positive variance on an income row is good", () => {
        expect(variantCellRole(120, true)).toBe("good");
    });

    test("negative variance on an income row is bad", () => {
        expect(variantCellRole(-120, true)).toBe("bad");
    });

    test("positive variance on an expense row is bad (cost rose)", () => {
        expect(variantCellRole(120, false)).toBe("bad");
    });

    test("negative variance on an expense row is good (cost fell)", () => {
        expect(variantCellRole(-120, false)).toBe("good");
    });

    test("no direction hint falls back to sign-only", () => {
        expect(variantCellRole(50, null)).toBe("good");
        expect(variantCellRole(-50, undefined)).toBe("bad");
    });

    test("zero is neutral/muted", () => {
        expect(variantCellRole(0, true)).toBe("muted");
    });
});
