/** @odoo-module **/

/**
 * Onboarding tour for SEPA Credit Transfer (PAIN.001) export.
 *
 * 5 steps explaining the four prerequisites a finance team has to
 * line up before a batch can produce a bank-acceptable XML file:
 *
 *   1. SEPA Originator config per bank journal (IBAN/BIC validated).
 *   2. Partner bank accounts on every payee (IBAN required).
 *   3. Outbound batch payment with posted lines.
 *   4. Single-currency rule (we refuse mixed-currency batches).
 *   5. Click Export — produces a downloadable .xml + audit row.
 *
 * Trigger: ?tour=eh_sepa_ct_setup_tour
 */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { markup } from "@odoo/owl";

registry.category("web_tour.tours").add("eh_sepa_ct_setup_tour", {
    url: "/odoo",
    steps: () => [
        {
            trigger: ".o_main_navbar",
            content: markup(_t(
                "Welcome to <b>SEPA Credit Transfer</b>. " +
                "This walkthrough shows the four prerequisites you " +
                "need before a batch can produce a bank-acceptable " +
                "PAIN.001 XML."
            )),
            run: () => {},
        },
        {
            trigger: ".o_main_navbar",
            content: markup(_t(
                "First: configure a <b>SEPA Originator</b> per bank " +
                "journal under Configuration. The originator carries " +
                "your IBAN, BIC, initiating-party name and an " +
                "optional party identifier (Spanish CIF, Italian " +
                "Codice Fiscale, etc.). The mod-97 IBAN check runs " +
                "on save so a typo is caught immediately."
            )),
            run: () => {},
        },
        {
            trigger: ".o_main_navbar",
            content: markup(_t(
                "Second: every <b>vendor</b> in the batch needs an " +
                "IBAN bank account on their partner record. The " +
                "export refuses to run when even one payee is " +
                "missing an IBAN — better to fix the data than to " +
                "have the bank reject the file."
            )),
            run: () => {},
        },
        {
            trigger: ".o_main_navbar",
            content: markup(_t(
                "Third: open <b>Batch Payments</b>, create an " +
                "outbound batch, enable <b>SEPA Credit Transfer</b>, " +
                "attach the vendor payments you " +
                "want to bundle, and post it. Posting a batch " +
                "locks the line set so the SEPA file matches " +
                "exactly what the GL sees."
            )),
            run: () => {},
        },
        {
            trigger: ".o_main_navbar",
            content: markup(_t(
                "Fourth: click <b>Export SEPA</b> on the posted " +
                "batch. The selected PAIN.001 version is structurally " +
                "validated, covered by official-XSD regression tests, and saved as an " +
                "ir.attachment. An <code>eh.sepa.export</code> " +
                "audit row links the message id, file hash, and " +
                "control sum back to the batch."
            )),
            run: () => {},
        },
    ],
});
