/** @odoo-module **/
// ============================================================================
// ERP Heritage
// Copyright (C) 2026 (https://www.erpheritage.com.au/)
// ============================================================================

// Report-local appearance preference. This deliberately stays client-only:
// switching a palette must never issue an RPC, invalidate a report cache, or
// alter exported/PDF accounting evidence.

export const REPORT_THEME_LIGHT = "light";
export const REPORT_THEME_DARK = "dark";
export const REPORT_THEME_STORAGE_PREFIX = "eh_account_dynamic_reports.theme.v1";

export function normalizeReportTheme(value) {
    return value === REPORT_THEME_LIGHT || value === REPORT_THEME_DARK
        ? value
        : null;
}

export function reportThemeDatabase(browserWindow = globalThis.window) {
    try {
        const odooInfo = browserWindow && browserWindow.odoo;
        const database = odooInfo && (
            (odooInfo.info && odooInfo.info.db)
            || (odooInfo.__session_info__ && odooInfo.__session_info__.db)
            || (odooInfo.session_info && odooInfo.session_info.db)
        );
        if (database) {
            return String(database);
        }
        const search = browserWindow && browserWindow.location
            ? String(browserWindow.location.search || "")
            : "";
        const match = search.match(/(?:^|[?&])db=([^&]+)/);
        return match ? decodeURIComponent(match[1]) : "default";
    } catch (_error) {
        return "default";
    }
}

export function reportThemeStorageKey(userId, database = "default") {
    const safeUserId = Number.isInteger(Number(userId)) && Number(userId) > 0
        ? String(Number(userId))
        : "anonymous";
    const safeDatabase = encodeURIComponent(String(database || "default"));
    return `${REPORT_THEME_STORAGE_PREFIX}.${safeDatabase}.${safeUserId}`;
}

export function cookieReportTheme(cookieValue) {
    const match = String(cookieValue || "").match(
        /(?:^|;\s*)color_scheme=(light|dark)(?:;|$)/,
    );
    return match ? match[1] : null;
}

export function resolveInitialReportTheme({
    storedTheme = null,
    cookieValue = "",
    prefersDark = false,
} = {}) {
    return normalizeReportTheme(storedTheme)
        || cookieReportTheme(cookieValue)
        || (prefersDark ? REPORT_THEME_DARK : REPORT_THEME_LIGHT);
}

export function loadReportTheme(
    userId,
    database = null,
    browserWindow = globalThis.window,
) {
    let storedTheme = null;
    try {
        storedTheme = browserWindow && browserWindow.localStorage
            ? browserWindow.localStorage.getItem(
                reportThemeStorageKey(
                    userId,
                    database || reportThemeDatabase(browserWindow),
                ),
            )
            : null;
    } catch (_error) {
        // Storage may be disabled by browser policy. Theme still resolves.
    }

    let prefersDark = false;
    try {
        prefersDark = Boolean(
            browserWindow
            && browserWindow.matchMedia
            && browserWindow.matchMedia("(prefers-color-scheme: dark)").matches,
        );
    } catch (_error) {
        // Old/embedded browsers fall back to light below.
    }

    let cookieValue = "";
    try {
        cookieValue = browserWindow && browserWindow.document
            ? browserWindow.document.cookie
            : "";
    } catch (_error) {
        // Cookie access can be unavailable in sandboxed webviews.
    }

    return resolveInitialReportTheme({ storedTheme, cookieValue, prefersDark });
}

export function saveReportTheme(
    theme,
    userId,
    database = null,
    browserWindow = globalThis.window,
) {
    const normalized = normalizeReportTheme(theme);
    if (!normalized) {
        return false;
    }
    try {
        if (!browserWindow || !browserWindow.localStorage) {
            return false;
        }
        browserWindow.localStorage.setItem(
            reportThemeStorageKey(
                userId,
                database || reportThemeDatabase(browserWindow),
            ),
            normalized,
        );
        return true;
    } catch (_error) {
        // A blocked/quota-full store must never break report rendering.
        return false;
    }
}
