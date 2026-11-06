/** @odoo-module **/

/**
 * Owl client action for the ERP Heritage financial dashboard.
 *
 * This component owns the live, reactive view of the dashboard. It is
 * registered against the action tag `eh_account_dashboard.board`, which
 * the model method `eh.account.dashboard.open_for_current_user` returns.
 *
 * Lifecycle:
 *
 *   onWillStart -> resolve the dashboard record id (from action context
 *                  or by calling open_for_current_user via JSON-RPC) and
 *                  load the first snapshot.
 *   onMounted   -> start the visible-tab auto-refresh clock (60s) and listen
 *                  for visibility changes.
 *   onWillUnmount -> clear the interval/listener after navigation.
 *
 * State shape:
 *   - snapshot: the latest payload from `get_dashboard_snapshot`. Null
 *     on first render before the RPC resolves.
 *   - loading:  true while a snapshot RPC is in flight.
 *   - error:    error message string if the last RPC failed.
 *
 * Drill-downs use the existing model action methods (action_drilldown_*)
 * via env.services.action.doActionButton, so the navigation behaviour
 * stays identical to the legacy form view.
 */

import { Component, onError, onMounted, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { sprintf } from "@web/core/utils/strings";

import { Sparkline } from "./sparkline";
import { KpiTile } from "./kpi_tile";

const REFRESH_INTERVAL_MS = 60_000;

export function shouldRunSilentRefresh(hidden, inFlight) {
    return !hidden && !inFlight;
}

export function customPeriodValidationError(dateFrom, dateTo) {
    if (!dateFrom || !dateTo) {
        return _t("Select both a start date and an end date.");
    }
    // Native date inputs emit ISO YYYY-MM-DD, whose lexical and chronological
    // ordering are identical. The server repeats this validation as the
    // authoritative boundary for non-UI callers.
    if (dateFrom > dateTo) {
        return _t("The start date must be on or before the end date.");
    }
    return null;
}

export class EhDashboard extends Component {
    static template = "eh_account_dashboard.Dashboard";
    static components = { Sparkline, KpiTile };
    static props = {
        action: { type: Object, optional: true },
        actionId: { type: [Number, Boolean], optional: true },
        className: { type: String, optional: true },
        globalState: { type: Object, optional: true },
        updateActionState: { type: Function, optional: true },
        "*": { optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");

        this.state = useState({
            snapshot: null,
            loading: true,
            error: null,
            recordId: null,
            periodMode: null,
            customDateFrom: "",
            customDateTo: "",
            customEditing: false,
        });

        this._intervalHandle = null;
        this._snapshotInFlight = false;
        this._onVisibilityChange = () => this._handleVisibilityChange();
        this.labels = Object.freeze({
            cashPosition: _t("Cash position"),
            receivables: _t("Receivables"),
            payables: _t("Payables"),
            revenue: _t("Revenue"),
            expense: _t("Expense"),
            netResult: _t("Net result"),
            pendingApprovals: _t("Pending approvals"),
            activeCollections: _t("Active collections"),
            activeBudgets: _t("Active budgets"),
            creditLimitSignals: _t("Credit-limit signals"),
            sepaDormancy: _t("SEPA dormancy"),
            periodClose: _t("Period close"),
            yearEndRuns: _t("Year-end runs"),
            fxRevaluation: _t("FX revaluation"),
            viewCashEntries: _t("View cash entries"),
            openReceivables: _t("Open receivables"),
            openPayables: _t("Open payables"),
            viewEntries: _t("View entries"),
            reviewQueue: _t("Review queue"),
            openWorkbench: _t("Open workbench"),
            viewBudgets: _t("View budgets"),
            overrideLog: _t("Override log"),
            reviewMandates: _t("Review mandates"),
            openRuns: _t("Open runs"),
            mandatesIdle: _t("Mandates idle over 33 months"),
            runsInProgress: _t("Runs in progress"),
            openOrUnposted: _t("Open or unposted"),
            computedUnposted: _t("Computed but unposted"),
        });

        onWillStart(async () => {
            await this._resolveRecordId();
            await this._loadSnapshot();
        });

        onMounted(() => {
            document.addEventListener(
                "visibilitychange", this._onVisibilityChange,
            );
            this._startAutoRefresh();
        });

        onWillUnmount(() => {
            document.removeEventListener(
                "visibilitychange", this._onVisibilityChange,
            );
            this._stopAutoRefresh();
        });

        // Error boundary: instead of letting an unexpected render
        // exception bubble to the global UncaughtPromiseError handler
        // (which presents a stack trace dialog), catch it locally and
        // surface a readable message inline. The original error is
        // attached to the cause for the browser console.
        onError((error) => {
            const cause = error?.cause || error;
            const message =
                cause?.data?.message
                || cause?.message
                || (typeof cause === "string" ? cause : "")
                || _t("The dashboard hit an unexpected error.");
            // eslint-disable-next-line no-console
            console.error("[eh_account_dashboard] render error:", error);
            this.state.error = message;
            this.state.loading = false;
        });
    }

    _startAutoRefresh() {
        if (this._intervalHandle || document.hidden) {
            return;
        }
        this._intervalHandle = window.setInterval(() => {
            if (shouldRunSilentRefresh(
                document.hidden, this._snapshotInFlight,
            )) {
                this._loadSnapshot({ silent: true });
            }
        }, REFRESH_INTERVAL_MS);
    }

    _stopAutoRefresh() {
        if (!this._intervalHandle) {
            return;
        }
        window.clearInterval(this._intervalHandle);
        this._intervalHandle = null;
    }

    _handleVisibilityChange() {
        if (document.hidden) {
            this._stopAutoRefresh();
            return;
        }
        this._startAutoRefresh();
        if (shouldRunSilentRefresh(false, this._snapshotInFlight)) {
            this._loadSnapshot({ silent: true });
        }
    }

    /**
     * Resolve the dashboard record id we should drive the snapshot
     * against. The action context may carry one (set by the server-side
     * action helper); otherwise we ask the model to find or create the
     * per-user record.
     */
    async _resolveRecordId() {
        const ctxId = this.props.action?.context?.eh_dashboard_id;
        if (ctxId) {
            this.state.recordId = ctxId;
            return;
        }
        const action = await this.orm.call(
            "eh.account.dashboard",
            "open_for_current_user",
            [],
        );
        const ctx = action?.context || {};
        if (ctx.eh_dashboard_id) {
            this.state.recordId = ctx.eh_dashboard_id;
        }
    }

    /**
     * Pull a fresh snapshot. `silent` skips the loading spinner so the
     * 60s auto-refresh does not flash the UI.
     */
    async _loadSnapshot({ silent = false } = {}) {
        if (
            this._snapshotInFlight
            || (silent && document.hidden)
        ) {
            return;
        }
        if (!this.state.recordId) {
            this.state.error = _t("No dashboard record available.");
            this.state.loading = false;
            return;
        }
        if (!silent) {
            this.state.loading = true;
        }
        this._snapshotInFlight = true;
        try {
            const snapshot = await this.orm.call(
                "eh.account.dashboard",
                "get_dashboard_snapshot",
                [[this.state.recordId]],
            );
            this.state.snapshot = snapshot;
            if (!this.state.customEditing) {
                this._syncPeriodControls(snapshot);
            }
            this.state.error = null;
        } catch (err) {
            this.state.error = err?.message?.message || _t("Failed to load dashboard.");
        } finally {
            this._snapshotInFlight = false;
            this.state.loading = false;
        }
    }

    onClickRefresh() {
        this._loadSnapshot();
    }

    /**
     * Always-array accessor for the sparkline. The snapshot may be
     * null on first render, may be missing the cash_trend key on
     * future server changes, or may carry it as null on tenants with
     * no cash journals; the Sparkline expects an Array, so we
     * normalise here once instead of leaning on QWeb expression
     * truthiness in the template.
     */
    get cashTrend() {
        const series = this.state.snapshot?.cash_trend;
        return Array.isArray(series) ? series : [];
    }

    get revenueTrend() {
        const series = this.state.snapshot?.revenue_trend;
        return Array.isArray(series) ? series : [];
    }

    get expenseTrend() {
        const series = this.state.snapshot?.expense_trend;
        return Array.isArray(series) ? series : [];
    }

    /**
     * Lookup a delta block from the snapshot. Returns an object with
     * .delta and .pct (formatted) suitable for KpiTile props, or an
     * empty object when no delta is available so the spread silently
     * omits the props.
     */
    deltaProps(key, { higherIsBetter = true } = {}) {
        const block = this.state.snapshot?.deltas?.[key];
        if (!block) return {};
        const props = {
            delta: block.delta,
            deltaLabel: this.formatMoney(block.delta),
            higherIsBetter,
        };
        // pct is null when the prior period is zero (e.g. a fresh
        // database): omit the prop entirely, because OWL rejects null
        // even on an optional [String, Number] prop.
        if (block.pct !== null && block.pct !== undefined) {
            props.deltaPct = block.pct;
        }
        return props;
    }

    async onChangePeriod(ev) {
        const mode = ev.target.value;
        this.state.periodMode = mode;
        if (mode === "custom") {
            const period = this.state.snapshot?.period || {};
            this.state.customDateFrom = period.date_from || "";
            this.state.customDateTo = period.date_to || "";
            this.state.customEditing = true;
            return;
        }
        this.state.customEditing = false;
        await this._writePeriod({ mode });
    }

    onCustomDateFrom(ev) {
        this.state.customDateFrom = ev.target.value;
    }

    onCustomDateTo(ev) {
        this.state.customDateTo = ev.target.value;
    }

    async onApplyCustomPeriod() {
        const error = customPeriodValidationError(
            this.state.customDateFrom,
            this.state.customDateTo,
        );
        if (error) {
            this.notification.add(error, { type: "warning" });
            return;
        }
        await this._writePeriod({
            mode: "custom",
            date_from: this.state.customDateFrom,
            date_to: this.state.customDateTo,
        });
    }

    async onTogglePosted(ev) {
        const mode = this.state.periodMode
            || this.state.snapshot?.period?.mode
            || "mtd";
        if (mode === "custom") {
            const error = customPeriodValidationError(
                this.state.customDateFrom,
                this.state.customDateTo,
            );
            if (error) {
                this.notification.add(error, { type: "warning" });
                ev.target.checked = !!this.state.snapshot?.period?.posted_only;
                return;
            }
        }
        await this._writePeriod({
            mode,
            date_from: mode === "custom" ? this.state.customDateFrom : null,
            date_to: mode === "custom" ? this.state.customDateTo : null,
            posted_only: ev.target.checked,
        });
    }

    _syncPeriodControls(snapshot) {
        const period = snapshot?.period || {};
        this.state.periodMode = period.mode || "mtd";
        this.state.customDateFrom = period.date_from || "";
        this.state.customDateTo = period.date_to || "";
    }

    async _writePeriod({ mode, date_from = null, date_to = null, posted_only }) {
        if (!this.state.recordId) return;
        const args = [
            [this.state.recordId],
            mode || "mtd",
            date_from || false,
            date_to || false,
            posted_only !== undefined
                ? posted_only
                : !!this.state.snapshot?.period?.posted_only,
        ];
        try {
            const snapshot = await this.orm.call(
                "eh.account.dashboard",
                "update_period",
                args,
            );
            this.state.snapshot = snapshot;
            this.state.customEditing = false;
            this._syncPeriodControls(snapshot);
            this.state.error = null;
        } catch (err) {
            this.notification.add(
                err?.message?.message || _t("Could not update period."),
                { type: "danger" },
            );
        }
    }

    async onClickDrilldown(methodName) {
        if (!this.state.recordId || !methodName) return;
        try {
            const action = await this.orm.call(
                "eh.account.dashboard",
                methodName,
                [[this.state.recordId]],
            );
            if (!action) {
                this.notification.add(
                    _t("This drill-down is not available: the related module is not installed."),
                    { type: "warning" },
                );
                return;
            }
            await this.action.doAction(action);
        } catch (err) {
            const detail = err?.data?.message
                || err?.data?.name
                || err?.message?.message
                || err?.message
                || _t("Drill-down failed.");
            this.notification.add(detail, { type: "danger" });
        }
    }

    /**
     * Format a monetary value using the snapshot currency. Avoids a
     * dependency on Odoo's monetary widget so the component renders
     * identically with or without a focused locale.
     */
    formatMoney(value) {
        const snap = this.state.snapshot;
        if (!snap) return "";
        const currency = snap.currency || {};
        const decimals = Number.isInteger(currency.decimal_places)
            ? currency.decimal_places
            : 2;
        const num = Number(value || 0).toLocaleString(undefined, {
            minimumFractionDigits: decimals,
            maximumFractionDigits: decimals,
        });
        const symbol = currency.symbol || "";
        if (currency.position === "before") {
            return `${symbol}${num}`;
        }
        return `${num} ${symbol}`.trim();
    }

    formatInt(value) {
        return Number(value || 0).toLocaleString();
    }

    cashAccountSecondary(count) {
        return sprintf(_t("%s cash account(s)"), this.formatInt(count));
    }

    receivableSecondary(liquidity) {
        if (!liquidity.receivable_overdue) {
            return _t("Nothing overdue");
        }
        return sprintf(
            _t("Overdue %s · oldest %s days"),
            this.formatMoney(liquidity.receivable_overdue),
            this.formatInt(liquidity.receivable_days_overdue_max),
        );
    }

    payableSecondary(liquidity) {
        return liquidity.payable_overdue
            ? sprintf(
                _t("Overdue %s"),
                this.formatMoney(liquidity.payable_overdue),
            )
            : _t("Nothing overdue");
    }

    pendingApprovalSecondary(count) {
        return count ? _t("Awaiting a decision") : _t("Queue is clear");
    }

    collectionsSecondary(total) {
        return sprintf(_t("Total overdue %s"), this.formatMoney(total));
    }

    budgetSecondary(overrunCount) {
        return overrunCount
            ? sprintf(_t("%s in overrun"), this.formatInt(overrunCount))
            : _t("All within budget");
    }

    creditOverrideSecondary(count) {
        return sprintf(
            _t("%s overrides / 30 days"), this.formatInt(count),
        );
    }

    /**
     * Map a P&L net to a status class so the tile colour reflects sign.
     */
    netStatus(value) {
        if (!value) return "eh_dash_status_neutral";
        return value >= 0 ? "eh_dash_status_ok" : "eh_dash_status_danger";
    }

    /**
     * Aggregate the controls block into one badge so the alerts strip
     * can render without each per-control number visible. Returns null
     * if the snapshot is not loaded yet.
     */
    get controlsBadgeClass() {
        const total = this.state.snapshot?.controls?.total || 0;
        if (!total) return "eh_dash_status_ok";
        return total > 5 ? "eh_dash_status_danger" : "eh_dash_status_warn";
    }

    /**
     * Build the "needs attention" rail from the snapshot's native
     * accounting-hygiene counters (documents, bank ops, integrity).
     * Each item carries a tone (mint when clear, warm when it needs
     * work), an icon, and the drill-down method to open the matching
     * records. Items whose count is zero stay visible but dimmed so the
     * rail reads as a control panel, not a disappearing to-do list.
     */
    get attentionItems() {
        const s = this.state.snapshot;
        if (!s) return [];
        const docs = s.documents || {};
        const bank = s.bank_ops || {};
        const integ = s.integrity || {};
        const row = (count, def) => ({
            ...def,
            count: count || 0,
            tone: count ? def.activeTone : "neutral",
        });
        return [
            row(docs.late_invoice_count, {
                key: "late_invoices",
                label: _t("Overdue customer invoices"),
                sub: _t("Posted, past due, unpaid"),
                icon: "fa-exclamation-circle",
                activeTone: "danger",
                drill: "action_drilldown_late_invoices",
            }),
            row(docs.late_bill_count, {
                key: "late_bills",
                label: _t("Overdue vendor bills"),
                sub: _t("Posted, past due, unpaid"),
                icon: "fa-exclamation-circle",
                activeTone: "warn",
                drill: "action_drilldown_late_bills",
            }),
            row(bank.to_reconcile_count, {
                key: "to_reconcile",
                label: _t("Statement lines to reconcile"),
                sub: _t("Bank lines awaiting a match"),
                icon: "fa-random",
                activeTone: "info",
                drill: "action_drilldown_to_reconcile",
            }),
            row(bank.to_check_count, {
                key: "to_check",
                label: _t("Entries to review"),
                sub: _t("Posted but unverified"),
                icon: "fa-check-square-o",
                activeTone: "info",
                drill: "action_drilldown_to_check",
            }),
            row(docs.draft_invoice_count, {
                key: "draft_invoices",
                label: _t("Draft customer invoices"),
                sub: _t("Billing backlog to confirm"),
                icon: "fa-file-text-o",
                activeTone: "warn",
                drill: "action_drilldown_draft_invoices",
            }),
            row(docs.draft_bill_count, {
                key: "draft_bills",
                label: _t("Draft vendor bills"),
                sub: _t("Entry backlog to confirm"),
                icon: "fa-file-text-o",
                activeTone: "warn",
                drill: "action_drilldown_draft_bills",
            }),
            row(integ.sequence_hole_count, {
                key: "sequence_holes",
                label: _t("Sequence gaps"),
                sub: _t("Missing numbers in posted journals"),
                icon: "fa-unlink",
                activeTone: "warn",
                drill: null,
            }),
            row(integ.unhashed_entry_count, {
                key: "unhashed",
                label: _t("Unsecured entries"),
                sub: _t("Posted without an inalterable hash"),
                icon: "fa-shield",
                activeTone: "warn",
                drill: null,
            }),
        ];
    }

    /** Count of attention items that currently need action. */
    get attentionOutstanding() {
        return this.attentionItems.filter((i) => i.count > 0).length;
    }

    /**
     * Ratio categories from the snapshot's 'ratios' payload block.
     * Always an array so the template loop never guards.
     */
    get ratioCategories() {
        const block = this.state.snapshot?.ratios;
        if (!block || !block.available) return [];
        return Array.isArray(block.categories) ? block.categories : [];
    }

    get ratioProvenanceItems() {
        const flags = this.state.snapshot?.ratios?.flags;
        if (!flags) return [];
        const interest = {
            tag: _t("Interest: IAS 7 account tag"),
            heuristic: _t("Interest: name heuristic"),
            none: _t("Interest: no account detected"),
        };
        const tax = {
            heuristic: _t("Income tax: name heuristic"),
            none: _t("Income tax: no account detected"),
        };
        return [
            {
                key: "interest",
                label: interest[flags.interest_source] || interest.none,
                tone: flags.interest_source === "tag" ? "ok"
                    : (flags.interest_source === "heuristic" ? "warn" : "neutral"),
            },
            {
                key: "tax",
                label: tax[flags.tax_source] || tax.none,
                tone: flags.tax_source === "heuristic" ? "warn" : "neutral",
            },
            {
                key: "inventory",
                label: flags.inventory_detected
                    ? _t("Inventory: name heuristic")
                    : _t("Inventory: no account detected"),
                tone: flags.inventory_detected ? "warn" : "neutral",
            },
        ];
    }

    /**
     * Map the server-side ratio status ('ok' / 'warn' / 'info' / 'na')
     * onto the existing tile status palette.
     */
    ratioStatusClass(ratio) {
        switch (ratio.status) {
            case "ok":
                return "eh_dash_status_ok";
            case "warn":
                return "eh_dash_status_warn";
            case "info":
                return "eh_dash_status_info";
            default:
                return "eh_dash_status_neutral";
        }
    }

    /**
     * Format a ratio value by its payload 'format' hint: 'pct' and
     * 'days' get a unit suffix, plain ratios get an 'x' multiple.
     * Null values (guarded denominators) render as 'n/a'; the note
     * explains why.
     */
    formatRatioValue(ratio) {
        if (ratio.value === null || ratio.value === undefined) {
            return _t("n/a");
        }
        const num = Number(ratio.value).toLocaleString(undefined, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
        });
        if (ratio.format === "pct") return `${num}%`;
        if (ratio.format === "days") return sprintf(_t("%s days"), num);
        return `${num}x`;
    }

    /** "+12.5% vs prior" badge text, empty when no baseline. */
    ratioDeltaLabel(ratio) {
        if (ratio.delta_pct === null || ratio.delta_pct === undefined) {
            return "";
        }
        const sign = ratio.delta_pct > 0 ? "+" : "";
        return sprintf(_t("%s vs prior"), `${sign}${ratio.delta_pct}%`);
    }
}

registry.category("actions").add("eh_account_dashboard.board", EhDashboard);
