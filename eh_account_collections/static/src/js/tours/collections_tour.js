/** @odoo-module **/

/**
 * Functional onboarding tour for Collections Workbench.
 *
 * The tour opens the real workbench action, enters a real case, and opens the
 * late-fee dialog. Every explanation is therefore anchored to a control the
 * operator can use, and the same path is exercised by an HttpCase regression.
 */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { markup } from "@odoo/owl";

registry.category("web_tour.tours").add("eh_collections_setup_tour", {
    url: "/odoo/action-eh_account_collections.action_eh_collections_case",
    steps: () => [
        {
            trigger: ".o_kanban_view",
            content: markup(_t(
                "This is the <b>Collections Workbench</b>. The nightly job " +
                "opens one case for each commercial customer whose oldest " +
                "receivable is beyond the company's configured grace days."
            )),
            run: () => {},
        },
        {
            trigger: ".o_kanban_record:not(.o_kanban_ghost)",
            content: markup(_t(
                "Open a case to review its live overdue balance, promise, " +
                "follow-up level, and immutable contact history."
            )),
            run: "click",
        },
        {
            trigger: ".o_form_view button[name='action_refresh_overdue']",
            content: markup(_t(
                "<b>Refresh Overdue</b> reads the posted receivable ledger " +
                "again. The nightly refresh uses the same aggregation."
            )),
            run: () => {},
        },
        {
            trigger: ".o_form_view .o_field_widget[name='action_ids'] .o_list_view",
            content: markup(_t(
                "The <b>Action Log</b> is append-only. Calls, reminders, " +
                "promises, escalations, and issued late-fee invoices remain " +
                "linked to their original actor and evidence."
            )),
            run: () => {},
        },
        {
            trigger: ".o_form_view button[name='action_open_late_fee_wizard']",
            content: markup(_t(
                "Use <b>Issue Late Fee</b> to preview a flat or prorated fee " +
                "from the live journal items. Duplicate issuance against the " +
                "same exposure, policy, and issue date is blocked."
            )),
            run: "click",
        },
        {
            trigger: ".modal .o_field_widget[name='computed_fee']",
            content: markup(_t(
                "Review the live exposure, fee, account, and taxes here. " +
                "Issuing creates a customer invoice and an immutable action " +
                "row; Cancel leaves both untouched."
            )),
            run: () => {},
        },
        {
            trigger: ".modal footer .btn-secondary",
            run: "click",
        },
        {
            trigger: ".o_form_view button[name='action_send_followup_now']",
            content: markup(_t(
                "<b>Send Follow-up</b> applies the highest overdue ladder " +
                "level not already handled. Configure email, SMS, activity " +
                "responsibility, and escalation targets under Follow-up " +
                "Levels."
            )),
            run: () => {},
        },
    ],
});
