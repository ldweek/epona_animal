/** @odoo-module **/
// ============================================================================
// ERP Heritage
// Copyright (C) 2026 (https://www.erpheritage.com.au/)
// ============================================================================
//
// Pure table-craft logic for the dynamic report viewer (WS5).
//
// These helpers carry NO OWL / service / DOM dependency so they are unit
// testable in isolation (hoot) and the component just delegates to them.
// They implement the three composable stages of the windowed renderer:
//
//   1. computeFilterKeepSet  - the in-table search match set (step 1)
//   2. (fold-visibility lives in the component; it needs childLines state)
//   3. sliceWindow           - the virtual-scroll window (step 3)
//   4. variantCellRole       - the good/bad/muted semantic for a value
//
// Keeping them pure means the fragile maths (divide-by-row-height, ancestor
// walks, sign-by-direction) is verified without a rendered component.

export const ROW_HEIGHT = 28;
export const OVERSCAN = 8;
export const DEFAULT_VIEWPORT_PX = 900;

// ---- step 1: in-table search keep-set ----
//
// Given the flat ordered payload lines and a lowercase query, return the Set
// of line ids to keep: every direct name match, PLUS its full ancestor chain
// (context above), PLUS the descendants of any directly-matched group (so a
// matched section reveals its rows). Returns null when the query is empty,
// meaning "no filter, keep everything".
export function computeFilterKeepSet(lines, rawQuery) {
    const q = (rawQuery || "").trim().toLowerCase();
    if (!q || !lines || !lines.length) {
        return null;
    }
    const byId = new Map();
    for (const line of lines) {
        byId.set(line.id, line);
    }
    const directMatch = new Set();
    for (const line of lines) {
        if ((line.name || "").toLowerCase().includes(q)) {
            directMatch.add(line.id);
        }
    }
    const keep = new Set();
    for (const id of directMatch) {
        keep.add(id);
        let parentId = byId.get(id) && byId.get(id).parent_id;
        const seen = new Set();
        while (parentId && !seen.has(parentId)) {
            seen.add(parentId);
            keep.add(parentId);
            const parent = byId.get(parentId);
            parentId = parent ? parent.parent_id : null;
        }
    }
    for (const line of lines) {
        if (keep.has(line.id)) {
            continue;
        }
        let parentId = line.parent_id;
        const seen = new Set();
        while (parentId && !seen.has(parentId)) {
            seen.add(parentId);
            if (directMatch.has(parentId)) {
                keep.add(line.id);
                break;
            }
            const parent = byId.get(parentId);
            parentId = parent ? parent.parent_id : null;
        }
    }
    return keep;
}

// ---- step 3: virtual-scroll window ----
//
// Slice the ordered visible array to the rows intersecting the viewport plus
// overscan. Returns { lines, total, startIndex, topPad, bottomPad }. Fixed
// row height keeps startIndex = floor(scrollTop / rowHeight) exact. FALLBACK:
// a non-positive rowHeight (degenerate) or empty list renders the full list
// with zero spacers, so the table never blanks or divides by zero.
export function sliceWindow(visible, opts) {
    const lines = visible || [];
    const total = lines.length;
    const rowHeight = (opts && opts.rowHeight != null)
        ? opts.rowHeight
        : ROW_HEIGHT;
    const overscan = (opts && opts.overscan != null) ? opts.overscan : OVERSCAN;
    if (!(rowHeight > 0) || total === 0) {
        return { lines, total, startIndex: 0, topPad: 0, bottomPad: 0 };
    }
    const scrollTop = Math.max(0, (opts && opts.scrollTop) || 0);
    const viewportPx = (opts && opts.viewportPx) || DEFAULT_VIEWPORT_PX;
    const viewportRows = Math.ceil(viewportPx / rowHeight);
    let startIndex = Math.floor(scrollTop / rowHeight) - overscan;
    if (startIndex < 0) {
        startIndex = 0;
    }
    let endIndex = startIndex + viewportRows + 2 * overscan;
    if (endIndex > total) {
        endIndex = total;
    }
    if (startIndex > endIndex) {
        startIndex = Math.max(0, endIndex - 1);
    }
    return {
        lines: lines.slice(startIndex, endIndex),
        total,
        startIndex,
        topPad: startIndex * rowHeight,
        bottomPad: (total - endIndex) * rowHeight,
    };
}

// ---- variance / comparison cell semantics ----
//
// Classify a numeric value in a comparison column as favourable (good),
// unfavourable (bad), or neutral (muted). `higherIsBetter` may be:
//   true  -> a rise is favourable (income/revenue),
//   false -> a fall is favourable (expense/cost),
//   null/undefined -> no directional hint: fall back to sign-only (positive
//                     is good), which is never worse than the prior
//                     all-negatives-red behaviour.
export function variantCellRole(value, higherIsBetter) {
    if (typeof value !== "number") {
        return null;
    }
    if (value === 0) {
        return "muted";
    }
    let favourable;
    if (higherIsBetter === true) {
        favourable = value > 0;
    } else if (higherIsBetter === false) {
        favourable = value < 0;
    } else {
        favourable = value > 0;
    }
    return favourable ? "good" : "bad";
}
