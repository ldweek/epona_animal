/** @odoo-module **/
// ============================================================================
// ERP Heritage
// Copyright (C) 2026 (https://www.erpheritage.com.au/)
// ============================================================================
//
// Bank reconciliation workspace.
//
// A two pane OWL component that drives the entire reconciliation flow:
//   * Top: journal picker + session counters + Close session button.
//   * Left pane: list of unreconciled statement lines for the selected
//     journal. Click to select.
//   * Right pane: scored suggestions for the selected line, plus action
//     buttons (Match, Skip, Write off).
//
// All state is local to the component; backend calls go through the
// session model's load_workspace, get_suggestions_for_line, apply_match,
// apply_skip methods. After every decision the workspace re-fetches the
// list so the just-matched line drops out and counters update.

import { Component, onWillStart, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { sprintf } from "@web/core/utils/strings";
import { user } from "@web/core/user";

// Server exceptions Odoo raises for the user to read (its error_dialogs
// registry shows them as warnings). Any other server exception keeps the
// generic title plus its class name, so a crash never prints its internals.
const USER_FACING_EXCEPTIONS = new Set([
    "odoo.exceptions.AccessDenied",
    "odoo.exceptions.AccessError",
    "odoo.exceptions.MissingError",
    "odoo.exceptions.RedirectWarning",
    "odoo.exceptions.UserError",
    "odoo.exceptions.ValidationError",
    "odoo.addons.web.controllers.action.MissingActionError",
    "odoo.addons.base.models.ir_actions.ServerActionWithWarningsError",
]);

// A failed server call carries the generic "Odoo Server Error" as its
// message; the text the user can act on is in data.message (data.arguments
// for a RedirectWarning). Prefer it so the workspace never hides why
// an action failed.
export function errorMessage(error) {
    const fallback = (error && error.message) || String(error);
    const data = error && error.data;
    if (!data || typeof data !== "object" || typeof data.name !== "string") {
        return fallback;
    }
    const exceptionClass = (data.context && data.context.exception_class) || "";
    if (USER_FACING_EXCEPTIONS.has(data.name)
            || USER_FACING_EXCEPTIONS.has(exceptionClass)) {
        const first = Array.isArray(data.arguments) ? data.arguments[0] : null;
        const text = data.name === "odoo.exceptions.RedirectWarning"
            && typeof first === "string" ? first : data.message;
        return typeof text === "string" && text.trim() ? text : fallback;
    }
    return `${fallback} (${data.name.split(".").pop()})`;
}

function fmtMoney(value) {
    if (value === null || value === undefined || value === "") return "";
    if (typeof value !== "number") return String(value);
    const abs = Math.abs(value).toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    });
    return value < 0 ? "(" + abs + ")" : abs;
}

function fmtPercent(value) {
    if (typeof value !== "number") return "";
    return Math.round(value * 100) + "%";
}

export class EhReconcileWorkspace extends Component {
    static template = "eh_account_reconcile_pro.ReconcileWorkspace";
    static props = { "*": true };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        // Odoo 19 removed the "user" service.
        this.user = user;

        this.state = useState({
            loading: true,
            error: null,
            journals: [],
            selectedJournalId: null,
            session: null,
            statementLines: [],
            selectedLineId: null,
            suggestions: [],
            suggestionsLoading: false,
            candidateSearch: "",
            statementSearch: "",
            statementDirection: "",
            statementDateFrom: "",
            statementDateTo: "",
            workspaceOffset: 0,
            workspaceLimit: 100,
            pagination: {
                offset: 0,
                limit: 100,
                total: 0,
                has_previous: false,
                has_next: false,
            },
            writeoffAccounts: [],
            selectedWriteoffAccountId: null,
            writeoffLabel: "",
            prediction: null,
            // aml ids ticked for a bulk apply, and the drop-target highlight
            // flag for drag and drop.
            selectedAmlIds: [],
            dragActive: false,
        });

        onWillStart(async () => await this.bootstrap());
    }

    async bootstrap() {
        this.state.loading = true;
        try {
            const journals = await this.orm.searchRead(
                "account.journal",
                [["type", "in", ["bank", "cash"]]],
                ["id", "name", "code", "type"],
                { order: "sequence asc, name asc" },
            );
            this.state.journals = journals;
            if (journals.length === 1) {
                await this.selectJournal(journals[0].id);
            } else {
                this.state.loading = false;
            }
        } catch (err) {
            this.state.error = errorMessage(err);
            this.state.loading = false;
        }
    }

    async selectJournal(journalId, options = {}) {
        const resetFilters = options.resetFilters !== false;
        this.state.loading = true;
        this.state.error = null;
        this.state.selectedJournalId = journalId;
        this.state.selectedLineId = null;
        this.state.suggestions = [];
        this.state.candidateSearch = "";
        this.state.selectedAmlIds = [];
        this.state.dragActive = false;
        if (resetFilters) {
            this.state.statementSearch = "";
            this.state.statementDirection = "";
            this.state.statementDateFrom = "";
            this.state.statementDateTo = "";
            this.state.workspaceOffset = 0;
        }
        try {
            const data = await this.orm.call(
                "eh.reconciliation.session",
                "load_workspace",
                [journalId, this.state.workspaceOffset,
                    this.state.workspaceLimit,
                    this.state.statementSearch.trim(),
                    this.state.statementDateFrom || false,
                    this.state.statementDateTo || false,
                    this.state.statementDirection || null],
            );
            this.state.session = data.session;
            this.state.statementLines = data.statement_lines;
            this.state.pagination = data.pagination;
            this.state.writeoffAccounts = await this.orm.call(
                "eh.reconciliation.session",
                "get_writeoff_options",
                [[data.session.id]],
            );
        } catch (err) {
            this.state.error = errorMessage(err);
        } finally {
            this.state.loading = false;
        }
    }

    async selectLine(lineId) {
        if (this.state.selectedLineId === lineId) {
            return;
        }
        this.state.selectedLineId = lineId;
        this.state.suggestionsLoading = true;
        this.state.suggestions = [];
        this.state.candidateSearch = "";
        // A bulk selection only makes sense for one statement line; reset
        // it whenever the user moves to a different line.
        this.state.selectedAmlIds = [];
        this.state.prediction = null;
        await this.loadSuggestions();
    }

    async loadSuggestions() {
        if (!this.state.selectedLineId) {
            return;
        }
        this.state.suggestionsLoading = true;
        try {
            const searchTerm = this.state.candidateSearch.trim();
            const [suggestions, prediction] = await Promise.all([
                this.orm.call(
                    "eh.reconciliation.session",
                    "get_suggestions_for_line",
                    [this.state.selectedLineId, searchTerm ? 50 : 10, 0.3, searchTerm],
                ),
                this.orm.call(
                    "eh.reconciliation.session",
                    "get_counterpart_prediction",
                    [[this.state.session.id], this.state.selectedLineId],
                ),
            ]);
            this.state.suggestions = suggestions;
            this.state.prediction = prediction;
        } catch (err) {
            this.notification.add(
                errorMessage(err),
                { type: "danger" },
            );
        } finally {
            this.state.suggestionsLoading = false;
        }
    }

    async searchCandidates(event) {
        if (event && event.type === "keydown" && event.key !== "Enter") {
            return;
        }
        if (event) {
            event.preventDefault();
        }
        this.state.selectedAmlIds = [];
        await this.loadSuggestions();
    }

    async searchStatements(event) {
        if (event && event.type === "keydown" && event.key !== "Enter") {
            return;
        }
        if (event) event.preventDefault();
        this.state.workspaceOffset = 0;
        await this.selectJournal(this.state.selectedJournalId, {
            resetFilters: false,
        });
    }

    async onStatementDirectionChange(event) {
        this.state.statementDirection = event.target.value;
        await this.searchStatements(event);
    }

    async previousStatementPage() {
        this.state.workspaceOffset = Math.max(
            0, this.state.workspaceOffset - this.state.workspaceLimit,
        );
        await this.selectJournal(this.state.selectedJournalId, {
            resetFilters: false,
        });
    }

    async nextStatementPage() {
        this.state.workspaceOffset += this.state.workspaceLimit;
        await this.selectJournal(this.state.selectedJournalId, {
            resetFilters: false,
        });
    }

    async match(amlId, source) {
        if (!this.state.selectedLineId || !this.state.session) {
            return;
        }
        const sessionId = this.state.session.id;
        try {
            const result = await this.orm.call(
                "eh.reconciliation.session",
                "apply_match",
                [[sessionId], this.state.selectedLineId, [amlId], source || "manual"],
            );
            const actualSource = result.source || source || "manual";
            this.notification.add(
                sprintf(_t("Matched (source: %s)"), actualSource),
                { type: "success" },
            );
            await this.refreshAfterDecision();
        } catch (err) {
            this.notification.add(
                errorMessage(err),
                { type: "danger" },
            );
        }
    }

    // ---- bulk selection ----

    isAmlSelected(amlId) {
        return this.state.selectedAmlIds.includes(amlId);
    }

    selectedAmlCount() {
        return this.state.selectedAmlIds.length;
    }

    paginationEnd() {
        return Math.min(
            this.state.pagination.offset + this.state.statementLines.length,
            this.state.pagination.total,
        );
    }

    toggleAmlSelection(amlId) {
        // Reassign a fresh array so the OWL reactivity proxy observes the
        // change; mutating in place with push/splice is not reliably seen.
        if (this.state.selectedAmlIds.includes(amlId)) {
            this.state.selectedAmlIds = this.state.selectedAmlIds.filter(
                (id) => id !== amlId,
            );
        } else {
            this.state.selectedAmlIds = [...this.state.selectedAmlIds, amlId];
        }
    }

    async matchSelected() {
        // Bulk match: reconcile the selected statement line against every
        // ticked candidate in ONE apply_match call (source "bulk"), instead
        // of one RPC per candidate.
        if (!this.state.selectedLineId || !this.state.session) {
            return;
        }
        const amlIds = this.state.selectedAmlIds;
        if (!amlIds.length) {
            return;
        }
        const sessionId = this.state.session.id;
        try {
            await this.orm.call(
                "eh.reconciliation.session",
                "apply_match",
                [[sessionId], this.state.selectedLineId, amlIds, "bulk"],
            );
            this.notification.add(
                sprintf(
                    _t("Matched %s item(s) (source: bulk)"), amlIds.length,
                ),
                { type: "success" },
            );
            this.state.selectedAmlIds = [];
            await this.refreshAfterDecision();
        } catch (err) {
            this.notification.add(
                errorMessage(err),
                { type: "danger" },
            );
        }
    }

    // ---- drag and drop ----

    onSuggestionDragStart(event, amlId) {
        // Carry the candidate AML id in the drag payload. text/plain has the
        // widest browser support.
        event.dataTransfer.setData("text/plain", String(amlId));
        event.dataTransfer.effectAllowed = "move";
    }

    onDropZoneDragOver(event) {
        // preventDefault is required for the element to accept a drop.
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        if (!this.state.dragActive) {
            this.state.dragActive = true;
        }
    }

    onDropZoneDragLeave() {
        this.state.dragActive = false;
    }

    onDropZoneDrop(event) {
        event.preventDefault();
        this.state.dragActive = false;
        const amlId = parseInt(event.dataTransfer.getData("text/plain"), 10);
        if (!isNaN(amlId)) {
            this.match(amlId, "drag_drop");
        }
    }

    async skip() {
        if (!this.state.selectedLineId || !this.state.session) {
            return;
        }
        const sessionId = this.state.session.id;
        try {
            await this.orm.call(
                "eh.reconciliation.session",
                "apply_skip",
                [[sessionId], this.state.selectedLineId],
            );
            this.notification.add(_t("Skipped for now"), { type: "info" });
            await this.refreshAfterDecision();
        } catch (err) {
            this.notification.add(
                errorMessage(err),
                { type: "danger" },
            );
        }
    }

    onWriteoffAccountChange(event) {
        const id = parseInt(event.target.value, 10);
        this.state.selectedWriteoffAccountId = isNaN(id) ? null : id;
    }

    async applyWriteoff() {
        if (!this.state.selectedLineId || !this.state.session ||
                !this.state.selectedWriteoffAccountId) {
            this.notification.add(_t("Select a write-off account first"), {
                type: "warning",
            });
            return;
        }
        try {
            await this.orm.call(
                "eh.reconciliation.session",
                "apply_write_off",
                [[this.state.session.id], this.state.selectedLineId,
                    this.state.selectedWriteoffAccountId,
                    this.state.writeoffLabel || null],
            );
            this.notification.add(_t("Write-off posted"), { type: "success" });
            await this.refreshAfterDecision();
        } catch (err) {
            this.notification.add(
                errorMessage(err), { type: "danger" },
            );
        }
    }

    async applyFxWriteoff() {
        if (!this.state.selectedLineId || !this.state.session) return;
        try {
            await this.orm.call(
                "eh.reconciliation.session",
                "apply_fx_writeoff",
                [[this.state.session.id], this.state.selectedLineId],
            );
            this.notification.add(_t("FX residual written off"), {
                type: "success",
            });
            await this.refreshAfterDecision();
        } catch (err) {
            this.notification.add(
                errorMessage(err), { type: "danger" },
            );
        }
    }

    async autoReconcile() {
        if (!this.state.session) return;
        try {
            const result = await this.orm.call(
                "eh.reconciliation.session",
                "action_auto_reconcile",
                [[this.state.session.id]],
            );
            this.notification.add(
                sprintf(
                    _t(
                        "Auto reconciliation cleared %s line(s); ambiguous " +
                        "lines were left for review",
                    ),
                    result.reconciled,
                ),
                { type: "success" },
            );
            await this.refreshAfterDecision();
        } catch (err) {
            this.notification.add(
                errorMessage(err), { type: "danger" },
            );
        }
    }

    async closeSession() {
        if (!this.state.session) return;
        const sessionId = this.state.session.id;
        try {
            await this.orm.call(
                "eh.reconciliation.session",
                "action_close",
                [[sessionId]],
            );
            this.notification.add(_t("Session closed"), { type: "success" });
            this.state.session = null;
            this.state.statementLines = [];
            this.state.selectedLineId = null;
            this.state.suggestions = [];
            this.state.selectedJournalId = null;
            await this.bootstrap();
        } catch (err) {
            this.notification.add(
                errorMessage(err),
                { type: "danger" },
            );
        }
    }

    async refreshAfterDecision() {
        if (this.state.selectedJournalId) {
            await this.selectJournal(this.state.selectedJournalId, {
                resetFilters: false,
            });
        }
    }

    onJournalChange(event) {
        const id = parseInt(event.target.value, 10);
        if (!isNaN(id)) {
            this.selectJournal(id);
        }
    }

    // ---- presentation helpers ----

    fmtMoney(v) { return fmtMoney(v); }
    fmtPercent(v) { return fmtPercent(v); }

    selectedLine() {
        const id = this.state.selectedLineId;
        if (!id) return null;
        return this.state.statementLines.find((sl) => sl.id === id) || null;
    }

    rulesFiredText(rules) {
        return Array.isArray(rules) ? rules.join(", ") : "";
    }

    confidenceColor(score) {
        if (score >= 0.85) return "eh_rec_conf_high";
        if (score >= 0.6) return "eh_rec_conf_mid";
        return "eh_rec_conf_low";
    }
}

registry.category("actions").add(
    "eh_reconcile_workspace", EhReconcileWorkspace,
);
