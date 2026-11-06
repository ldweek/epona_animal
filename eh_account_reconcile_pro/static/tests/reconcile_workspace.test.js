/** @odoo-module **/
// ============================================================================
// ERP Heritage
// Copyright (C) 2026 (https://www.erpheritage.com.au/)
// ============================================================================

import { describe, expect, test } from "@odoo/hoot";
import {
    EhReconcileWorkspace,
    errorMessage,
} from "@eh_account_reconcile_pro/components/reconcile_workspace/reconcile_workspace";

describe("eh reconcile workspace: a failed server call shows its own message", () => {
    const serverText = "This statement line is already reconciled.";

    function serverError(name = "odoo.exceptions.UserError", data = {}) {
        const rpcError = new Error("Odoo Server Error");
        rpcError.data = { name, message: serverText, arguments: [serverText], ...data };
        return rpcError;
    }

    test("a user error shows the exception text, not the generic title", () => {
        expect(errorMessage(serverError())).toBe(serverText);
        expect(errorMessage(serverError("odoo.exceptions.ValidationError"))).toBe(serverText);
        expect(errorMessage(serverError("odoo.exceptions.AccessError"))).toBe(serverText);
    });

    test("a redirect warning shows its message argument only", () => {
        const redirect = serverError("odoo.exceptions.RedirectWarning", {
            message: serverText + "\n42\nOpen settings",
            arguments: [serverText, 42, "Open settings"],
        });
        expect(errorMessage(redirect)).toBe(serverText);
    });

    test("a subclass that Odoo flags as a user error shows its text", () => {
        const subclass = serverError("odoo.addons.some_module.models.SomeError", {
            context: { exception_class: "odoo.exceptions.UserError" },
        });
        expect(errorMessage(subclass)).toBe(serverText);
    });

    test("an unexpected server exception keeps the generic title with its class", () => {
        const crash = serverError("builtins.KeyError", { message: "'account_code'" });
        expect(errorMessage(crash)).toBe("Odoo Server Error (KeyError)");
        const sql = serverError("psycopg2.errors.UndefinedColumn", {
            message: 'column "x" does not exist\nLINE 1: SELECT x FROM account_move',
        });
        expect(errorMessage(sql)).toBe("Odoo Server Error (UndefinedColumn)");
    });

    test("a client error or a blank server text keeps the error message", () => {
        expect(errorMessage(new Error("Connection lost"))).toBe("Connection lost");
        const blank = serverError("odoo.exceptions.UserError", { message: "  " });
        expect(errorMessage(blank)).toBe("Odoo Server Error");
        expect(errorMessage("plain text")).toBe("plain text");
        expect(errorMessage(null)).toBe("null");
    });

    test("every workspace action shows the server text of its failure", async () => {
        const actions = [
            ["bootstrap", [], "error"],
            ["selectJournal", [7], "error"],
            ["loadSuggestions", [], "notification"],
            ["match", [5, "manual"], "notification"],
            ["matchSelected", [], "notification"],
            ["skip", [], "notification"],
            ["applyWriteoff", [], "notification"],
            ["applyFxWriteoff", [], "notification"],
            ["autoReconcile", [], "notification"],
            ["closeSession", [], "notification"],
        ];
        for (const [method, args, surface] of actions) {
            const notes = [];
            const workspace = {
                state: {
                    loading: false,
                    error: null,
                    selectedLineId: 3,
                    selectedJournalId: 7,
                    session: { id: 9 },
                    selectedAmlIds: [5],
                    selectedWriteoffAccountId: 11,
                    writeoffLabel: "",
                    candidateSearch: "",
                    statementSearch: "",
                    workspaceOffset: 0,
                    workspaceLimit: 50,
                },
                notification: { add(message) { notes.push(message); } },
                orm: {
                    async call() { throw serverError(); },
                    async searchRead() { throw serverError(); },
                },
            };
            await EhReconcileWorkspace.prototype[method].call(workspace, ...args);
            const shown = surface === "error" ? workspace.state.error : notes[0];
            expect(`${method}: ${shown}`).toBe(`${method}: ${serverText}`);
        }
    });
});
