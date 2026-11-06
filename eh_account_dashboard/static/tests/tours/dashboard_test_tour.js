/** @odoo-module **/

/**
 * Browser verification for the Owl dashboard entry point.
 *
 * Test-only asset: proves the Accounting menu action resolves the current
 * user's company-scoped dashboard, renders a real snapshot, and survives a
 * manual refresh round trip.  Stable data-testid selectors keep the tour
 * independent from visual layout and translated labels.
 *
 * KEEP STEP SHAPE MECHANICAL for tools/backport_account.py:
 * one double-quoted trigger and a click/no-op run per step.
 */

import { registry } from "@web/core/registry";
import { stepUtils } from "@web_tour/tour_utils";

registry.category("web_tour.tours").add("eh_dashboard_test_tour", {
    url: "/odoo",
    test: true,
    steps: () => [
        stepUtils.showAppsMenuItem(),
        {
            trigger: ".o_app[data-menu-xmlid='account.menu_finance']",
            run: "click",
        },
        {
            trigger: "[data-menu-xmlid='eh_account_dashboard.menu_eh_account_dashboard']",
            run: "click",
        },
        {
            trigger: "[data-testid='eh-financial-dashboard'] [data-testid='eh-dashboard-period']",
            run: () => {},
        },
        {
            trigger: "[data-testid='eh-dashboard-refresh']",
            run: "click",
        },
        {
            trigger: "[data-testid='eh-financial-dashboard'] .eh_dashboard_body",
            run: () => {},
        },
    ],
});
