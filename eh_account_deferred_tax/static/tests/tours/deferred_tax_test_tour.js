/** @odoo-module **/

/**
 * Automated verification tour for the deferred tax run (IFRS 10/10
 * program, UI layer). Unlike onboarding tours under static/src, this file
 * ships in web.assets_tests only: it never loads for real users and
 * exists solely for HttpCase.start_tour runs in the version matrix.
 *
 * KEEP THE STEP SHAPE MECHANICAL. tools/backport_account.py rewrites test
 * tours for the older series with a line-based transform, so every step
 * must stay in the exact  { trigger: "...", run: "..." }  form below:
 *   - selectors: double-quoted, one per step, prefer [data-menu-xmlid=...]
 *   - actions:   run: "click" | "edit <text>" | () => {}
 * The 16 target converts  registry -> tour.register  and  edit -> text;
 * 16/17 targets convert the /odoo url to /web.
 */

import { registry } from "@web/core/registry";
import { stepUtils } from "@web_tour/tour_utils";

registry.category("web_tour.tours").add("eh_deferred_tax_test_tour", {
    url: "/odoo",
    test: true,
    steps: () => [
        stepUtils.showAppsMenuItem(),
        {
            trigger: ".o_app[data-menu-xmlid='account.menu_finance']",
            run: "click",
        },
        {
            trigger: "[data-menu-xmlid='account.menu_finance_entries']",
            run: "click",
        },
        {
            trigger: ".dropdown-item[data-menu-xmlid='eh_account_deferred_tax.menu_eh_deferred_tax_run']",
            run: "click",
        },
        {
            trigger: ".o_list_button_add",
            run: "click",
        },
        {
            trigger: ".o_field_widget[name='statutory_rate'] input",
            run: "edit 30.0",
        },
        {
            trigger: ".o_field_x2many_list_row_add a",
            run: "click",
        },
        {
            trigger: ".o_selected_row .o_field_widget[name='name'] input",
            run: "edit Accelerated depreciation",
        },
        {
            trigger: ".o_selected_row .o_field_widget[name='carrying_amount'] input",
            run: "edit 1000.00",
        },
        {
            trigger: ".o_selected_row .o_field_widget[name='tax_base'] input",
            run: "edit 600.00",
        },
        {
            trigger: ".o_form_button_save",
            run: "click",
        },
        {
            trigger: ".o_form_saved",
            run: () => {},
        },
        {
            trigger: "button[name='action_compute']",
            run: "click",
        },
        {
            trigger: "button[name='action_post']",
            run: () => {},
        },
    ],
});
