/** @odoo-module **/

/**
 * Onboarding tour for the Bank Reconciliation Workspace.
 *
 * 5 steps walking through the engine's distinguishing features:
 * the suggestion engine, the rule editor, the audit log, and the
 * exception report. Trigger: ?tour=eh_reconcile_setup_tour.
 */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { markup } from "@odoo/owl";

registry.category("web_tour.tours").add("eh_reconcile_setup_tour", {
    url: "/odoo/action-eh_account_reconcile_pro.action_eh_reconcile_workspace",
    steps: () => [
        {
            trigger: ".eh_rec",
            content: markup(_t(
                "Welcome to <b>Bank Reconciliation Pro</b>. " +
                "This walkthrough covers the suggestion engine, " +
                "the rule editor, the audit log, and the daily " +
                "exception report."
            )),
            run: "click",
        },
        {
            trigger: ".eh_rec_select",
            content: markup(_t(
                "Open the <b>Workspace</b> from the Bank " +
                "Reconciliation menu. The workspace lists every " +
                "unmatched statement line on the journal; the " +
                "suggestion engine scores candidate journal items " +
                "by amount, date, partner, reference, and history."
            )),
            run: () => {
                const select = document.querySelector(".eh_rec_select");
                if (select && select.options.length > 1) {
                    select.selectedIndex = 1;
                    select.dispatchEvent(new Event("change", { bubbles: true }));
                }
            },
        },
        {
            trigger: ".eh_rec_statement_search input",
            content: markup(_t(
                "Configure <b>Reconciliation Rules</b> under " +
                "Configuration. A rule fires when its regex on " +
                "payment_ref or narration matches and the line " +
                "amount falls in the configured band; matching " +
                "rules add a confidence boost so the engine ranks " +
                "the right candidate first."
            )),
            run: "click",
        },
        {
            trigger: ".eh_rec_sl",
            content: markup(_t(
                "Every match, write-off, and skip is recorded in " +
                "the <b>Reconciliation Decisions</b> audit log. " +
                "The log is append-only; you cannot edit or " +
                "delete a decision once posted, so the trail is " +
                "legally defensible."
            )),
            run: "click",
        },
        {
            trigger: ".eh_rec_candidate_search input",
            content: markup(_t(
                "Print the <b>Exception Report</b> daily for " +
                "supervisor review. It shows total / reconciled / " +
                "unmatched line counts per journal, the oldest " +
                "unmatched line, and a colour-coded health badge " +
                "(green at least 90%, amber at least 70%, red below)."
            )),
            run: "click",
        },
    ],
});
