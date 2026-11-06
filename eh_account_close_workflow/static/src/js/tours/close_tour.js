/** @odoo-module **/

/**
 * Functional onboarding tour for Period Close Workflow.
 *
 * Every explanation is anchored to a shipped control: the tour opens a real
 * run, starts it, inspects its copied task rows, and opens the mandatory Block
 * reason dialog. The accompanying HttpCase proves these selectors remain live.
 */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { markup } from "@odoo/owl";

registry.category("web_tour.tours").add("eh_close_setup_tour", {
    url: "/odoo/action-eh_account_close_workflow.action_eh_close_run",
    steps: () => [
        {
            trigger: ".o_kanban_view",
            content: markup(_t(
                "This is the <b>Period Close</b> workbench. Each card owns " +
                "one bounded company period; cards cannot be dragged between " +
                "guarded workflow states."
            )),
            run: () => {},
        },
        {
            trigger: ".o_kanban_record:not(.o_kanban_ghost)",
            content: markup(_t(
                "Open a run to work the independent task copies seeded from " +
                "its checklist."
            )),
            run: "click",
        },
        {
            trigger: ".o_form_view button[name='action_start']",
            content: markup(_t(
                "<b>Start</b> records the current-cycle preparer in the " +
                "append-only sign-off history."
            )),
            run: "click",
        },
        {
            trigger: ".o_form_view .o_field_widget[name='task_ids'] .o_list_view",
            content: markup(_t(
                "Required tasks must finish before a different user can " +
                "request approval. Optional tasks remain visibly advisory."
            )),
            run: () => {},
        },
        {
            trigger: ".o_form_view .o_field_widget[name='task_ids'] button[name='action_open_block_wizard']",
            content: markup(_t(
                "Use <b>Block</b> when a dependency stops work. The dialog " +
                "requires a material reason for the reviewer."
            )),
            run: "click",
        },
        {
            trigger: ".modal .o_field_widget[name='reason'] textarea",
            content: markup(_t(
                "Describe the dependency, owner, and expected resolution. " +
                "Confirming preserves the reason on the task; Cancel makes " +
                "no change."
            )),
            run: () => {},
        },
        {
            trigger: ".modal footer .btn-secondary",
            run: "click",
        },
        {
            trigger: ".o_form_view button[name='action_request_approval']",
            content: markup(_t(
                "<b>Request Approval</b> re-runs the live ledger checks and " +
                "enforces a reviewer different from the preparer. A third, " +
                "different manager performs final approval and any configured " +
                "Global Lock advance."
            )),
            run: () => {},
        },
    ],
});
