/** @odoo-module **/

/**
 * Automated verification tour for the fixed asset register (IFRS 10/10
 * program, UI layer). Unlike the onboarding tours under static/src, this
 * file ships in web.assets_tests only: it never loads for real users and
 * exists solely for HttpCase.start_tour runs in the version matrix.
 *
 * KEEP THE STEP SHAPE MECHANICAL. tools/backport_account.py rewrites test
 * tours for the older series with a line-based transform, so every step
 * must stay in the exact  { trigger: "...", run: "..." }  form below:
 *   - selectors: double-quoted, one per step, prefer [data-menu-xmlid=...]
 *   - actions:   run: "click" | "edit <text>" | () => {}
 * The 16 target converts  registry -> tour.register  and  edit -> text;
 * 16/17 targets convert the /odoo url to /web.
 *
 * The wrapping Python test pre-creates the "Tour Plant Category" asset
 * category so the required category_id autocomplete has a deterministic
 * match. The asset name is sequence-assigned (readonly '/') and is never
 * edited here. Compute Schedule is draft-safe: it only builds persisted
 * schedule lines and posts nothing, so no accounts are required.
 */

import { registry } from "@web/core/registry";
import { stepUtils } from "@web_tour/tour_utils";

registry.category("web_tour.tours").add("eh_assets_test_tour", {
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
            trigger: ".dropdown-item[data-menu-xmlid='eh_account_assets_pro.menu_eh_asset_all']",
            run: "click",
        },
        {
            trigger: ".o_list_button_add",
            run: "click",
        },
        {
            trigger: ".o_field_widget[name='code'] input",
            run: "edit TOUR-FA-01",
        },
        {
            trigger: ".o_field_widget[name='category_id'] input",
            run: "edit Tour Plant Category",
        },
        {
            trigger: ".o-autocomplete--dropdown-item a:contains('Tour Plant Category')",
            run: "click",
        },
        {
            trigger: ".o_field_widget[name='acquisition_cost'] input",
            run: "edit 12000.00",
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
            trigger: "button[name='action_compute_schedule']",
            run: "click",
        },
        {
            trigger: ".o_field_widget[name='depreciation_line_ids'] .o_data_row",
            run: () => {},
        },
    ],
});
