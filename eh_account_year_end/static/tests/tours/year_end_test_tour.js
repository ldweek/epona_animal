/** @odoo-module **/

/**
 * Automated verification tour for the year-end closing run (IFRS 10/10
 * program, UI layer). Unlike onboarding tours under static/src, this
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
 * The run action lands on kanban first (view_mode kanban,list,form), so
 * the create step clicks .o-kanban-button-new (same class on 16-19).
 * The required journal and retained earnings accounts are pre-seeded as
 * ir.default records by tests/test_tour.py, so the tour only edits the
 * two plain date inputs. The run name is sequence-assigned (readonly '/')
 * from the fiscal year-end date and is never edited here. The final step
 * proves the draft saved successfully; compute/post/reverse accounting
 * behavior is exercised by the module's transaction tests, not fabricated
 * in a browser fixture without a seeded ledger.
 */

import { registry } from "@web/core/registry";
import { stepUtils } from "@web_tour/tour_utils";

registry.category("web_tour.tours").add("eh_year_end_test_tour", {
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
            trigger: ".dropdown-item[data-menu-xmlid='eh_account_year_end.menu_eh_year_end_run']",
            run: "click",
        },
        {
            trigger: ".o_switch_view.o_list",
            run: "click",
        },
        {
            trigger: ".o_list_button_add",
            run: "click",
        },
        {
            trigger: ".o_field_widget[name='fiscal_year_start'] .o_input",
            run: "click",
        },
        {
            trigger: ".o_field_widget[name='fiscal_year_start'] input",
            run: "edit 01/01/2024",
        },
        {
            trigger: ".o_field_widget[name='fiscal_year_end'] .o_input",
            run: "click",
        },
        {
            trigger: ".o_field_widget[name='fiscal_year_end'] input",
            run: "edit 12/31/2024",
        },
        {
            trigger: ".o_form_button_save",
            run: "click",
        },
        {
            trigger: ".o_form_saved",
            run: () => {},
        },
    ],
});
