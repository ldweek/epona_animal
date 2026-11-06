/** @odoo-module **/

import { cookie } from "@web/core/browser/cookie";
import { user } from "@web/core/user";
import { browser } from "@web/core/browser/browser";
import { registry } from "@web/core/registry";

const COLOR_SCHEME_COOKIE = "color_scheme";

/** 'schedule' preference: dark inside the user's time window (defaults
 *  19:00–07:00; the hour fields ship with the Pro theme). Handles windows
 *  that wrap past midnight. */
function scheduleIsDark() {
    const from = Number(user.settings.ht_dark_from ?? 19);
    const until = Number(user.settings.ht_dark_until ?? 7);
    if (from === until) {
        return false; // empty window — never dark
    }
    const h = new Date().getHours();
    return from < until ? h >= from && h < until : h >= from || h < until;
}

function resolveScheme(preference) {
    if (preference === "auto") {
        return browser.matchMedia("(prefers-color-scheme: dark)").matches
            ? "dark"
            : "light";
    }
    if (preference === "schedule") {
        return scheduleIsDark() ? "dark" : "light";
    }
    return preference === "dark" ? "dark" : "light";
}

/**
 * PER-USER colour scheme (Pro upgrade over the free theme's global one).
 * Each user's choice (Light / Dark / System) lives in res.users.settings;
 * this service keeps the ``color_scheme`` cookie in sync with it. The free
 * theme's global service stands down when Pro is installed.
 */
export const colorSchemeUserService = {
    start() {
        const preference = user.settings.color_scheme || "light";
        const effectiveScheme = resolveScheme(preference);
        const currentCookie = cookie.get(COLOR_SCHEME_COOKIE);

        if (effectiveScheme !== currentCookie) {
            cookie.set(COLOR_SCHEME_COOKIE, effectiveScheme);
            browser.location.reload();
            return;
        }

        if (preference === "auto") {
            const mediaQuery = browser.matchMedia(
                "(prefers-color-scheme: dark)"
            );
            mediaQuery.addEventListener("change", () => {
                const newScheme = resolveScheme("auto");
                if (newScheme !== cookie.get(COLOR_SCHEME_COOKIE)) {
                    cookie.set(COLOR_SCHEME_COOKIE, newScheme);
                    browser.location.reload();
                }
            });
        }

        if (preference === "schedule") {
            // Re-check once a minute: crossing a boundary (e.g. 19:00) flips
            // the cookie and reloads so the dark bundle swaps in.
            browser.setInterval(() => {
                const newScheme = resolveScheme("schedule");
                if (newScheme !== cookie.get(COLOR_SCHEME_COOKIE)) {
                    cookie.set(COLOR_SCHEME_COOKIE, newScheme);
                    browser.location.reload();
                }
            }, 60000);
        }
    },
};

registry
    .category("services")
    .add("home_theme.dark_scheme", colorSchemeUserService);
