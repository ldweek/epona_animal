/** @odoo-module **/
// ============================================================================
// ERP Heritage
// Copyright (C) 2026 (https://www.erpheritage.com.au/)
// ============================================================================
//
// Dynamic Report viewer.
//
// A client action that renders any registered eh.account.dynamic.report
// interactively in the Odoo backend. The action context provides the
// report_code; the component fetches the report record, calls render() to
// get the JSON payload, and lays it out as a hierarchical table with a
// comprehensive filter pane above.
//
// Filters: date mode (range, as-of, this/last month/quarter/year),
// comparison toggle (none, previous_period, previous_year), companies,
// journals, partners, accounts, account types, analytic plans and
// analytic accounts. Posted-only and show-zero stay as quick checkboxes.
//
// Currency: every payload carries a currency block resolved server side
// from the company scope. The viewer renders amounts with the right
// symbol, decimal places, position. Multi-currency scopes mark the
// payload as such and the cells render numbers without a symbol.
//
// Hierarchy: lines flagged unfoldable can be expanded / collapsed; the
// component tracks the expanded set in state so re-renders preserve it.

import {
    Component, onWillStart, onMounted, onWillUnmount, useState, useRef,
} from "@odoo/owl";
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";
import { sprintf } from "@web/core/utils/strings";
import { session } from "@web/session";
import { user } from "@web/core/user";
import {
    todayStr,
    errorMessage,
    firstOfMonthStr,
    PRESET_RANGES,
    shiftDateRange,
    ACCOUNT_TYPE_CHOICES,
    reportCapabilitiesForCode,
    unsupportedOptionsForCode,
    drilldownOptionsForColumn,
    effectiveFigureType,
    isDrillableReportLine,
    lineCellHasValue,
    isNumericFigureType,
    formatCurrency,
    normalizeColumnHeaderRows,
    allocatedAnalyticScopeForColumn,
    normalizeAnalyticDrilldownPage,
    normalizePositiveIdList,
} from "./report_format";
import { EhCellEllipsis } from "./cell_ellipsis";
import {
    ROW_HEIGHT,
    OVERSCAN,
    DEFAULT_VIEWPORT_PX,
    computeFilterKeepSet,
    sliceWindow,
    variantCellRole,
} from "./report_table_logic";
import {
    loadReportTheme,
    normalizeReportTheme,
    saveReportTheme,
} from "./report_theme";

// ---- virtual-scroll constants (WS5) ----
// ROW_HEIGHT / OVERSCAN / DEFAULT_VIEWPORT_PX are imported from the pure
// logic module (which the hoot suite tests in isolation). The .scss clamps
// every rendered row to ROW_HEIGHT so floor(scrollTop / ROW_HEIGHT) is exact
// and the window stays O(viewport) regardless of payload size.
//
// In-table search debounce. Client-side only (never hits the server), so it
// stays instant on a multi-year ledger; the debounce just avoids rebuilding
// the filtered set on every keystroke.
const SEARCH_DEBOUNCE_MS = 150;

// Virtual-scroll engages only when the visible row count exceeds this. Below
// it, the table renders as one plain <tbody> with no spacer rows, so the
// sticky <thead> pins reliably (spacer-row windowing was detaching the header
// into the body mid-scroll). The lazy engine keeps real reports well under
// this, so windowing is reserved for an exceptionally large expanded view.
const VIRTUAL_THRESHOLD = 4000;

export class EhDynamicReportViewer extends Component {
    static template = "eh_account_dynamic_reports.DynamicReportViewer";
    static components = { EhCellEllipsis };
    static props = { "*": true };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.dialog = useService("dialog");
        this.user = user;
        this.userId = (this.user && this.user.userId) || null;
        this.database = (session && session.db) || null;
        // The scroll container (.eh_dr_body). Its scrollTop / clientHeight
        // drive windowedLines(); we read them in onBodyScroll and on mount.
        this.bodyRef = useRef("body");
        // Fixed row height + overscan exposed to the template for spacer math.
        this.ROW_HEIGHT = ROW_HEIGHT;
        this._refreshSequence = 0;
        // Bumped whenever childLines is reset, so an expand_line response
        // started before the reset cannot write old children back in.
        this._childLinesSequence = 0;
        this._analyticDrilldownSequence = 0;

        const ctx = (this.props.action && this.props.action.context) || {};
        this.reportCode = ctx.report_code;

        this.state = useState({
            loading: true,
            // Client-only display preference. It never enters report options,
            // saved views, RPC payloads, render hashes, PDF, or XLSX output.
            theme: loadReportTheme(this.userId, this.database),
            error: null,
            reportId: null,
            reportName: "",
            payload: null,
            filtersExpanded: false,
            expandedLines: [],
            // Lazy expand: per-line fetched children, keyed by line id.
            // { [lineId]: { lines: [...], hasMore, nextOffset, loading,
            //   totalCount } }. Cleared on every refresh so a new payload
            // never shows stale children, and never persisted (lazy leaves
            // always start collapsed on reload, per the §2 invariant).
            childLines: {},
            savedViews: [],
            currentSavedViewId: null,
            savedViewDialog: null,
            savedViewName: "",
            savedViewShared: false,
            // Exact weighted analytic-cell detail. Null = closed. Backend
            // recomputes clicked scope from public options + expression;
            // caller-supplied private allocation keys are never trusted.
            analyticDrilldown: null,
            choices: {
                companies: [],
                journals: [],
                partners: [],
                accounts: [],
                accountTypes: ACCOUNT_TYPE_CHOICES,
                analyticPlans: [],
                analyticAccounts: [],
                currencies: [],
            },
            options: {
                date: {
                    mode: "range",
                    date_from: firstOfMonthStr(),
                    date_to: todayStr(),
                },
                company_ids: [],
                journal_ids: [],
                partner_ids: [],
                account_ids: [],
                account_type_ids: [],
                analytic_account_ids: [],
                analytic_plan_ids: [],
                posted_only: true,
                show_zero: false,
                comparison: "none",
                comparison_custom_date_from: "",
                comparison_custom_date_to: "",
                comparison_order: "descending",
                analytic_column_account_ids: [],
                analytic_column_plan_ids: [],
                presentation_currency_id: null,
                // Aged-report config (interval/bucket count/basis) and the
                // reconcile-state filter. Only the aged handlers read these;
                // other reports ignore them. They are part of the options
                // hash so a different bucket grid / reconcile set caches and
                // re-serves separately.
                aging_interval: 30,
                aging_bucket_count: 4,
                aging_basis: "maturity",
                reconcile_state: "open",
                // Opt into the server's lazy expand-on-demand path. Account
                // leaves arrive collapsed; their journal items are fetched
                // only when expanded. Export/PDF render eagerly server-side
                // and ignore this flag, so paper output is unchanged.
                lazy_expand: true,
                // Keep hierarchy state inside the saved-view contract. The
                // handler defaults to grouped rows when this key is absent,
                // but an explicit false value must survive save/load.
                hierarchical_groups: true,
                unfold_all: false,
                unfolded_lines: [],
                // WS3 ghost-feature options. All four pass straight through
                // render()->compute() and only change behaviour when set, so
                // the default values reproduce today's single-period,
                // standard-layout, direct-coarse-cash-flow output exactly.
                // comparison_number>1 widens to N side-by-side periods;
                // horizontal_group_by='company' pivots to per-company columns;
                // cash_flow_method/cash_flow_reconciled select the cash-flow
                // attribution. They are part of the options hash so each
                // variant caches and re-serves separately.
                comparison_number: 1,
                horizontal_group_by: null,
                cash_flow_method: "direct",
                cash_flow_reconciled: false,
                // IAS 7.31 presentation overrides. Empty string means
                // "follow the company policy" - the server resolves the
                // eh_cf_*_section fields on res.company, so the default
                // render is policy-true without any option noise.
                cf_interest_paid_section: "",
                cf_dividends_paid_section: "",
            },
            // Annotation popover state: which line/cell currently has its
            // note popover open ({lineId,label}) and the in-flight draft.
            // null = closed. Kept out of options so it never affects the
            // render hash or a saved view.
            annotationOpen: null,
            annotationDraft: "",
            // ---- WS5 table craft ----
            // In-table search. Pure client-side filter over the already
            // loaded payload (and any spliced lazy children); never sent to
            // the server, so it never re-queries the ledger and never enters
            // the options hash. Empty string = no filter.
            tableQuery: "",
            // Virtual-scroll viewport tracking. scrollTop is the live scroll
            // offset of .eh_dr_body; viewportPx is its measured clientHeight.
            // Both feed windowedLines(); kept in state so a scroll re-renders
            // only the visible window. Defaults are pre-measure fallbacks.
            scrollTop: 0,
            viewportPx: DEFAULT_VIEWPORT_PX,
        });

        onWillStart(async () => {
            await this.bootstrap();
        });

        onMounted(() => {
            // Measure the real viewport once the scroll container exists so
            // the very first windowed slice is sized to the actual screen
            // rather than the pre-measure fallback. Guarded: if the ref is
            // missing we keep the fallback, never throwing.
            this._measureViewport();
        });
        onWillUnmount(() => {
            clearTimeout(this._accountSearchTimer);
            clearTimeout(this._tableSearchTimer);
            // Invalidate any outstanding render response so it cannot write
            // into a component that has already left the action stack.
            this._refreshSequence += 1;
            this._childLinesSequence += 1;
        });
    }

    onThemeChange(theme) {
        const normalized = normalizeReportTheme(theme);
        if (!normalized || normalized === this.state.theme) {
            return;
        }
        this.state.theme = normalized;
        saveReportTheme(normalized, this.userId, this.database);
    }

    _measureViewport() {
        const el = this.bodyRef && this.bodyRef.el;
        if (el && el.clientHeight) {
            this.state.viewportPx = el.clientHeight;
        }
    }

    onBodyScroll(ev) {
        // Live scroll offset drives the window. Reading scrollTop off the
        // event target keeps it cheap; re-measuring clientHeight here too
        // covers a resize that happened since mount. Defensive: a missing
        // target leaves the prior offset untouched.
        const el = (ev && ev.target) || (this.bodyRef && this.bodyRef.el);
        if (!el) return;
        this.state.scrollTop = el.scrollTop || 0;
        if (el.clientHeight) {
            this.state.viewportPx = el.clientHeight;
        }
    }

    async bootstrap() {
        if (!this.reportCode) {
            this.state.loading = false;
            this.state.error = _t("No report code was provided by this action.");
            return;
        }
        const allowed = (this.user && this.user.context
            && this.user.context.allowed_company_ids) || [];
        if (allowed.length) {
            this.state.options.company_ids = allowed.slice();
        }
        let records;
        try {
            records = await this.orm.searchRead(
                "eh.account.dynamic.report",
                [["code", "=", this.reportCode]],
                ["id", "name"],
                { limit: 1 },
            );
        } catch (exc) {
            // Menus are role-gated, but a stale bookmark or a changed role can
            // still enter the client action. Keep that path as a readable
            // access message instead of an unhandled OWL startup rejection.
            this.state.loading = false;
            this.state.error =
                _t("You do not have access to this ERP Heritage report.");
            return;
        }
        if (!records.length) {
            this.state.loading = false;
            this.state.error = sprintf(
                _t("No registered report with code: %s"), this.reportCode,
            );
            return;
        }
        this.state.reportId = records[0].id;
        this.state.reportName = records[0].name;
        await this.loadFilterChoices();
        await this.loadSavedViews();
        await this.refresh();
    }

    async loadSavedViews() {
        try {
            const views = await this.orm.call(
                "eh.account.report.saved_view", "list_for",
                [this.reportCode],
            );
            this.state.savedViews = views || [];
        } catch (e) {
            this.state.savedViews = [];
        }
    }

    savedViewOptionDefaults() {
        // Extension modules should override this method, spread super, and
        // add defaults for every option they add to state. That keeps legacy
        // saved views isolated from whichever view was active previously.
        return {
            journal_ids: [],
            partner_ids: [],
            account_ids: [],
            account_type_ids: [],
            analytic_account_ids: [],
            analytic_plan_ids: [],
            posted_only: true,
            show_zero: false,
            comparison: "none",
            comparison_number: 1,
            comparison_custom_date_from: "",
            comparison_custom_date_to: "",
            comparison_order: "descending",
            analytic_column_account_ids: [],
            analytic_column_plan_ids: [],
            presentation_currency_id: null,
            aging_interval: 30,
            aging_bucket_count: 4,
            aging_basis: "maturity",
            reconcile_state: "open",
            lazy_expand: true,
            hierarchical_groups: true,
            unfold_all: false,
            unfolded_lines: [],
            horizontal_group_by: null,
            cash_flow_method: "direct",
            cash_flow_reconciled: false,
            cf_interest_paid_section: "",
            cf_dividends_paid_section: "",
        };
    }

    async onSavedViewChange(event) {
        const viewId = parseInt(event.target.value, 10);
        if (!viewId) {
            this.state.currentSavedViewId = null;
            return;
        }
        try {
            const opts = await this.orm.call(
                "eh.account.report.saved_view", "load_options",
                [[viewId]],
            );
            if (opts) {
                const cur = this.state.options;
                const absentKeyDefaults = this.savedViewOptionDefaults();
                for (const [key, defaultValue] of Object.entries(
                    absentKeyDefaults,
                )) {
                    if (key in cur && !(key in opts)) {
                        cur[key] = Array.isArray(defaultValue)
                            ? [...defaultValue] : defaultValue;
                    }
                }
                // Keep keys the viewer knows about and drop unknown keys from
                // the saved payload.
                for (const key of Object.keys(cur)) {
                    if (key in opts) {
                        cur[key] = opts[key];
                    }
                }
                this.state.currentSavedViewId = viewId;
                this.refresh();
            }
        } catch (e) {
            this.notification.add(
                sprintf(
                    _t("Failed to load saved view: %s"),
                    errorMessage(e),
                ),
                { type: "danger" },
            );
        }
    }

    onSaveCurrentView() {
        this.normalizeCapabilityOptions();
        this.state.savedViewDialog = "save";
        this.state.savedViewName = _t("My filters");
        this.state.savedViewShared = false;
    }

    onSavedViewNameInput(event) {
        this.state.savedViewName = event.target.value;
    }

    onSavedViewSharedToggle(event) {
        this.state.savedViewShared = !!event.target.checked;
    }

    onCloseSavedViewDialog() {
        this.state.savedViewDialog = null;
        this.state.savedViewName = "";
        this.state.savedViewShared = false;
    }

    async onConfirmSaveCurrentView() {
        const name = (this.state.savedViewName || "").trim();
        if (!name) return;
        try {
            const newId = await this.orm.call(
                "eh.account.report.saved_view", "save_view",
                [name, this.reportCode, this.state.options,
                    this.state.savedViewShared],
            );
            this.state.currentSavedViewId = newId;
            this.notification.add(
                sprintf(_t("Saved view: %s"), name), { type: "success" },
            );
            this.onCloseSavedViewDialog();
            await this.loadSavedViews();
        } catch (e) {
            this.notification.add(
                errorMessage(e), { type: "danger" },
            );
        }
    }

    onDeleteSavedView() {
        if (!this.state.currentSavedViewId) return;
        this.state.savedViewDialog = "delete";
    }

    async onConfirmDeleteSavedView() {
        if (!this.state.currentSavedViewId) return;
        try {
            await this.orm.unlink(
                "eh.account.report.saved_view",
                [this.state.currentSavedViewId],
            );
            this.state.currentSavedViewId = null;
            this.onCloseSavedViewDialog();
            await this.loadSavedViews();
        } catch (e) {
            this.notification.add(
                errorMessage(e), { type: "danger" },
            );
        }
    }

    async loadFilterChoices() {
        // Each filter feed is independent; if one access-rule trips
        // we still want the rest of the picker to work. Use tryFetch
        // (already fault-tolerant) for every model and default to []
        // so downstream .map / .find calls never see undefined.
        const [companies, journals, partners, accounts, plans, analyticAccounts, currencies] = await Promise.all([
            this.tryFetch(
                "res.company", [],
                ["id", "name", "fiscalyear_last_month", "fiscalyear_last_day"],
                { limit: 50, order: "name" },
            ),
            this.tryFetch("account.journal", [], ["id", "name", "code"], { limit: 200, order: "name" }),
            this.tryFetch(
                "res.partner",
                ["|", ["customer_rank", ">", 0],
                    ["supplier_rank", ">", 0], ["parent_id", "=", false]],
                ["id", "name"],
                { limit: 100, order: "name" },
            ),
            this.tryFetch("account.account", [], ["id", "code", "name"], { limit: 200, order: "code" }),
            this.tryFetch("account.analytic.plan", [], ["id", "name"], { limit: 50, order: "name" }),
            this.tryFetch("account.analytic.account", [], ["id", "name"], { limit: 100, order: "name" }),
            this.tryFetch("res.currency", [["active", "=", true]], ["id", "name", "symbol"], { limit: 200, order: "name" }),
        ]);
        this.state.choices.currencies = currencies || [];
        this.state.choices.companies = companies || [];
        this.state.choices.journals = (journals || []).map((j) => ({
            ...j, name: j.code ? `${j.code} ${j.name}` : j.name,
        }));
        this.state.choices.partners = partners || [];
        this.state.choices.accounts = accounts || [];
        this.state.choices.analyticPlans = plans || [];
        this.state.choices.analyticAccounts = analyticAccounts || [];
    }

    async tryFetch(model, domain, fields, opts) {
        // Analytic models may not be installed in some setups; tolerate.
        try {
            return await this.orm.searchRead(model, domain, fields, opts);
        } catch (e) {
            return [];
        }
    }

    onAccountSearch(event) {
        // Debounced server-side search so an arbitrary account (for
        // example one beyond the initial code-ordered page, routine on a
        // large chart of accounts) can be located by code or name instead
        // of scrolled for in a capped flat list.
        const term = event.target.value;
        clearTimeout(this._accountSearchTimer);
        this._accountSearchTimer = setTimeout(() => {
            this.searchAccounts(term);
        }, 300);
    }

    async searchAccounts(rawTerm) {
        const term = (rawTerm || "").trim();
        const domain = term
            ? ["|", ["code", "ilike", term], ["name", "ilike", term]]
            : [];
        const found = await this.tryFetch(
            "account.account", domain, ["id", "code", "name"],
            { limit: 200, order: "code" },
        );
        // Keep already-selected accounts in the option list so picking a
        // further match does not drop earlier selections (the multi-select
        // reports only its rendered-and-selected options) and so their
        // chips keep resolving to code and name.
        const selected = this.state.options.account_ids || [];
        const missing = selected.filter(
            (id) => !found.some((a) => a.id === id),
        );
        let extras = [];
        if (missing.length) {
            extras = await this.tryFetch(
                "account.account", [["id", "in", missing]],
                ["id", "code", "name"], { order: "code" },
            );
        }
        this.state.choices.accounts = [...extras, ...found];
    }

    async refresh() {
        if (!this.state.reportId) {
            return;
        }
        this.normalizeCapabilityOptions();
        const sequence = ++this._refreshSequence;
        this.onCloseAnalyticDrilldown();
        this.state.loading = true;
        this.state.error = null;
        try {
            const payload = await this.orm.call(
                "eh.account.dynamic.report",
                "render",
                [[this.state.reportId], this.state.options],
            );
            if (sequence !== this._refreshSequence) return;
            this.state.payload = payload;
            // A fresh payload invalidates any previously fetched children:
            // ids may have changed and lazy leaves always start collapsed.
            this.state.childLines = {};
            this._childLinesSequence += 1;
            // A new payload starts scrolled to the top so the window does not
            // point past the end of a shorter result; the prior in-table
            // search filter is also cleared (its matches referenced the old
            // line ids / names).
            this.state.scrollTop = 0;
            this.state.tableQuery = "";
            if (this.bodyRef && this.bodyRef.el) {
                this.bodyRef.el.scrollTop = 0;
            }
            // Per-user fold persistence: hydrate the user's saved
            // expand / collapse choices for this report so the next
            // render starts from the same shape as the previous
            // session. When no preferences exist yet (returns {}),
            // fall back to the default of "everything expanded".
            if (payload && Array.isArray(payload.lines)) {
                // Foldability is now decided server-side by the uniform fold
                // normalization (_eh_normalize_fold): a row is unfoldable IFF
                // it is a lazy leaf OR it has a child in the payload. The
                // viewer no longer flips section headers on the client, so a
                // flat header (cash-flow / executive-summary / an empty
                // bank-reconciliation section) gets NO stray caret while a
                // section that really nests rows stays foldable.
                const code = this.reportCode;
                let savedState = {};
                try {
                    if (code) {
                        savedState = await this.orm.call(
                            "eh.account.report.fold.state",
                            "get_for_user",
                            [code],
                        ) || {};
                        if (sequence !== this._refreshSequence) return;
                    }
                } catch (exc) {
                    // Persistence is a nice-to-have; never block the
                    // render on a fold-state RPC failure.
                    savedState = {};
                }
                const expanded = [];
                for (const line of payload.lines) {
                    if (!line.unfoldable) continue;
                    // NEVER auto-restore a lazy leaf's expansion. Re-expanding
                    // every account on reload would fan out to a fetch per
                    // account (the §2 invariant forbids it), so lazy leaves
                    // always start collapsed regardless of saved fold state.
                    if (line.lazy) continue;
                    let isExpanded;
                    if (line.id in savedState) {
                        isExpanded = !!savedState[line.id];
                    } else if ("__default__" in savedState) {
                        // Collapse all saved a report wide default.
                        isExpanded = !!savedState.__default__;
                    } else {
                        // Default: respect the handler's per-line
                        // unfolded flag (defaults to true).
                        isExpanded = line.unfolded !== false;
                    }
                    if (isExpanded) expanded.push(line.id);
                }
                this.state.expandedLines = expanded;
            }
        } catch (exc) {
            if (sequence === this._refreshSequence) {
                this.state.error = errorMessage(exc);
            }
        } finally {
            if (sequence === this._refreshSequence) {
                this.state.loading = false;
            }
        }
    }

    async onRefresh() {
        await this.refresh();
    }

    async onExportXlsx() {
        if (!this.state.reportId) return;
        this.normalizeCapabilityOptions();
        try {
            const action = await this.orm.call(
                "eh.account.dynamic.report",
                "export_xlsx_attachment",
                [[this.state.reportId], this.state.options],
            );
            await this.action.doAction(action);
        } catch (exc) {
            this.notification.add(
                errorMessage(exc), { type: "danger" },
            );
        }
    }

    async onPrintPdf() {
        if (!this.state.reportId) return;
        this.normalizeCapabilityOptions();
        try {
            const action = await this.orm.call(
                "eh.account.dynamic.report",
                "export_pdf_attachment",
                [[this.state.reportId], this.state.options],
            );
            await this.action.doAction(action);
        } catch (exc) {
            this.notification.add(
                errorMessage(exc), { type: "danger" },
            );
        }
    }

    async onLineClick(line, drilldownOptions = null) {
        if (!this.state.reportId) return;
        try {
            const drillAction = await this.orm.call(
                "eh.account.dynamic.report",
                "get_drilldown_for_line",
                [[this.state.reportId], drilldownOptions || this.state.options,
                    line.id],
            );
            if (drillAction) {
                await this.action.doAction(drillAction);
            }
        } catch (exc) {
            this.notification.add(
                sprintf(
                    _t("Could not open the selected journal items: %s"),
                    errorMessage(exc),
                ),
                { type: "danger" },
            );
        }
    }

    onAmountCellClick(line, column, valueIndex = null) {
        // WS1 gesture split: the legacy full-page drilldown (open the native
        // journal-items / move list) lives on the amount cell only. The caret
        // (t-on-click.stop) owns the inline lazy unfold, and a bare click on
        // the name cell does nothing, so the two gestures never collide.
        // Sentinel / load-more rows carry no drilldown.
        if (!this.isDrillableLine(line)) return;
        if (allocatedAnalyticScopeForColumn(column)) {
            if (this.state.options.cash_basis) return;
            const cell = Number.isInteger(valueIndex)
                && line && Array.isArray(line.columns)
                ? line.columns[valueIndex] : null;
            const displayedAmount = cell && cell.value;
            return this.onOpenAnalyticDrilldown(
                line, column, displayedAmount,
            );
        }
        if (this.hasGlobalAnalyticRowFilters()) return;
        const options = drilldownOptionsForColumn(
            this.state.options,
            column,
            (this.state.payload && this.state.payload.meta) || {},
        );
        if (!options) return;
        return this.onLineClick(line, options);
    }

    async onOpenAnalyticDrilldown(line, column, displayedAmount) {
        const executionId = this.state.payload
            && this.state.payload.execution_id;
        if (!this.state.reportId
                || !Number.isInteger(executionId)
                || executionId < 1
                || typeof displayedAmount !== "number"
                || !Number.isFinite(displayedAmount)
                || this.state.options.cash_basis
                || !allocatedAnalyticScopeForColumn(column)
                || typeof column.expression_label !== "string"
                || !column.expression_label.trim()) {
            return;
        }
        // The weighted-detail endpoint owns scope reconstruction and must
        // receive only the public report options.  Strip any stale private
        // overlay keys defensively before making the request.
        const cleanOptions = Object.fromEntries(Object.entries(
            this.state.options,
        ).filter(([key]) => !key.startsWith("_eh_")));
        const publicOptions = {
            ...cleanOptions,
            date: { ...(cleanOptions.date || {}) },
        };
        const token = ++this._analyticDrilldownSequence;
        this.state.analyticDrilldown = {
            token,
            title: sprintf(_t("Allocated detail: %s"), line.name || ""),
            columnName: column.name || "",
            lineId: line.id,
            expressionLabel: column.expression_label,
            requestOptions: publicOptions,
            executionId,
            displayedAmount,
            pageToken: null,
            columns: [],
            rows: [],
            total: 0,
            offset: 0,
            limit: 80,
            totalCount: 0,
            hasMore: false,
            currency: null,
            scope: null,
            loading: true,
            error: null,
        };
        await this._loadAnalyticDrilldownPage(false);
    }

    async _loadAnalyticDrilldownPage(append) {
        const detail = this.state.analyticDrilldown;
        if (!detail || (detail.loading && append)) return;
        detail.loading = true;
        detail.error = null;
        const offset = append ? detail.rows.length : 0;
        try {
            const rawPage = await this.orm.call(
                "eh.account.dynamic.report",
                "get_analytic_column_drilldown_page",
                [[this.state.reportId], detail.requestOptions,
                    detail.lineId, detail.expressionLabel,
                    offset, detail.limit, detail.executionId,
                    detail.displayedAmount,
                    append ? detail.pageToken : null],
            );
            const page = normalizeAnalyticDrilldownPage(rawPage);
            if (!page) {
                throw new Error(_t("Weighted detail response was malformed."));
            }
            if (page.offset !== offset || page.limit !== detail.limit) {
                throw new Error(_t("Weighted detail page was out of sequence."));
            }
            if (append && (
                page.total_count !== detail.totalCount
                || page.total !== detail.total
                || page.page_token !== detail.pageToken
                || !detail.currency
                || page.currency.id !== detail.currency.id
            )) {
                throw new Error(_t(
                    "Weighted detail changed while paging; reopen the cell.",
                ));
            }
            if (!this.state.analyticDrilldown
                    || this.state.analyticDrilldown.token !== detail.token) {
                return;
            }
            detail.columns = page.columns;
            detail.rows = append ? [...detail.rows, ...page.rows] : page.rows;
            detail.total = page.total;
            detail.offset = page.offset;
            detail.limit = page.limit;
            detail.totalCount = page.total_count;
            detail.hasMore = page.has_more;
            detail.pageToken = page.page_token;
            detail.currency = page.currency;
            detail.scope = page.scope;
        } catch (error) {
            if (this.state.analyticDrilldown
                    && this.state.analyticDrilldown.token === detail.token) {
                detail.error = errorMessage(error);
            }
        } finally {
            if (this.state.analyticDrilldown
                    && this.state.analyticDrilldown.token === detail.token) {
                detail.loading = false;
            }
        }
    }

    onLoadMoreAnalyticDrilldown() {
        return this._loadAnalyticDrilldownPage(true);
    }

    onCloseAnalyticDrilldown() {
        this._analyticDrilldownSequence =
            (this._analyticDrilldownSequence || 0) + 1;
        if (this.state && this.state.analyticDrilldown) {
            this.state.analyticDrilldown = null;
        }
    }

    analyticDrilldownCellValue(row, column) {
        const detail = this.state.analyticDrilldown;
        const value = row && row.values && row.values[column.key];
        if (column.figure_type === "monetary") {
            return formatCurrency(
                value,
                detail && detail.currency,
                "monetary",
                ((this.user && this.user.context && this.user.context.lang)
                    || "en_US").replace("_", "-"),
            );
        }
        return value === null || value === undefined ? "" : String(value);
    }

    analyticDrilldownTotal() {
        const detail = this.state.analyticDrilldown;
        if (!detail || !detail.currency) return "";
        return formatCurrency(
            detail.total, detail.currency, "monetary",
            ((this.user && this.user.context && this.user.context.lang)
                || "en_US").replace("_", "-"),
        );
    }

    onOpenAnalyticDrilldownMove(row) {
        if (!row || !row.move_id) return;
        return this.action.doAction({
            type: "ir.actions.act_window",
            name: _t("Journal Entry"),
            res_model: "account.move",
            res_id: row.move_id,
            view_mode: "form",
            views: [[false, "form"]],
            target: "current",
        });
    }

    onInteractiveKeydown(event, callback) {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        callback();
    }

    isDrillableLine(line) {
        return isDrillableReportLine(this.reportCode, line);
    }

    isDrillableColumn(line, column) {
        if (!lineCellHasValue(line, column, this.valueColumnDefs())) {
            return false;
        }
        const allocatedScope = allocatedAnalyticScopeForColumn(column);
        if (this.state.options.cash_basis && allocatedScope) {
            return false;
        }
        if (!allocatedScope && this.hasGlobalAnalyticRowFilters()) return false;
        return this.isDrillableLine(line) && Boolean(drilldownOptionsForColumn(
            this.state.options,
            column,
            (this.state.payload && this.state.payload.meta) || {},
        ));
    }

    hasGlobalAnalyticRowFilters() {
        const options = this.state.options || {};
        return Boolean(
            (Array.isArray(options.analytic_account_ids)
                && options.analytic_account_ids.length)
            || (Array.isArray(options.analytic_plan_ids)
                && options.analytic_plan_ids.length)
        );
    }

    // ---- filter handlers ----

    onToggleFilters() {
        this.state.filtersExpanded = !this.state.filtersExpanded;
    }

    onDateModeChange(event) {
        const mode = event.target.value;
        this.state.options.date.mode = mode;
        if (PRESET_RANGES[mode]) {
            const [from, to] = PRESET_RANGES[mode](this.fiscalConfig());
            this.state.options.date.date_from = from;
            this.state.options.date.date_to = to;
            this.refresh();
        } else if (mode === "as_of") {
            // A lower bound of year 1 turns every flow report into a lifetime
            // report and comparison math can underflow before year 1. Snapshot
            // handlers (Balance Sheet / aged reports) already implement their
            // own cumulative cutoff semantics server-side. Other handlers get
            // the only honest range behind an As-of control: the selected day.
            this.state.options.date.date_from =
                this.state.options.date.date_to;
            this.refresh();
        }
    }

    fiscalConfig() {
        const selected = (this.state.options.company_ids || [])[0];
        const company = this.state.choices.companies.find(
            (candidate) => candidate.id === selected,
        ) || this.state.choices.companies[0] || {};
        return {
            lastMonth: company.fiscalyear_last_month || 12,
            lastDay: company.fiscalyear_last_day || 31,
        };
    }

    onShiftPeriod(direction) {
        const date = this.state.options.date;
        const [from, to] = shiftDateRange(
            date.date_from, date.date_to, direction, date.mode,
        );
        date.date_from = from;
        date.date_to = to;
        this.refresh();
    }

    onDateFromChange(event) {
        this.state.options.date.date_from = event.target.value;
    }

    onDateToChange(event) {
        this.state.options.date.date_to = event.target.value;
    }

    onComparisonChange(event) {
        this.state.options.comparison = event.target.value;
        // Dropping back to "no comparison" makes a multi-period request
        // meaningless; reset the period count so the stale N never lingers
        // in a saved view or chip.
        if (this.state.options.comparison === "none") {
            this.state.options.comparison_number = 1;
            this.state.options.comparison_order = "descending";
        } else if (this.state.options.comparison === "custom") {
            this.state.options.comparison_number = 1;
            if (!this.state.options.comparison_custom_date_from) {
                this.state.options.comparison_custom_date_from =
                    this.state.options.date.date_from;
            }
            if (!this.state.options.comparison_custom_date_to) {
                this.state.options.comparison_custom_date_to =
                    this.state.options.date.date_to;
            }
        }
        if (this.state.options.comparison !== "custom") {
            this.state.options.comparison_custom_date_from = "";
            this.state.options.comparison_custom_date_to = "";
        }
        this.normalizeComparisonBudgetOptions();
        this.refresh();
    }

    onComparisonCustomDateFromChange(event) {
        this.state.options.comparison_custom_date_from = event.target.value;
    }

    onComparisonCustomDateToChange(event) {
        this.state.options.comparison_custom_date_to = event.target.value;
    }

    onComparisonOrderChange(event) {
        this.state.options.comparison_order = event.target.value === "ascending"
            ? "ascending" : "descending";
        this.refresh();
    }

    // ---- WS3 ghost-feature controls ----

    // Capability ownership is deliberately centralized in the static map.
    // A handler-emitted ghost branch used to make UI availability depend on
    // whichever payload happened to finish last.
    reportCapabilities() {
        return reportCapabilitiesForCode(this.reportCode);
    }

    supportsComparison() {
        return this.reportCapabilities().includes("comparison");
    }

    supportsAccountTypes() {
        return this.reportCapabilities().includes("account_types");
    }

    unsupportedOptions() {
        const staticOptions = unsupportedOptionsForCode(this.reportCode);
        const fromMeta = this.state.payload
            && this.state.payload.meta
            && this.state.payload.meta.unsupported_option_keys;
        if (Array.isArray(fromMeta)) {
            return [...new Set([...staticOptions, ...fromMeta])];
        }
        return staticOptions;
    }

    supportsOption(key) {
        return !this.unsupportedOptions().includes(key);
    }

    normalizeCapabilityOptions() {
        // Saved views may outlive handler capabilities. Remove unsupported
        // values before render/export/save so hidden controls never become
        // ghost cache dimensions or silently alter RPC payloads.
        if (!this.supportsComparison()) {
            this.state.options.comparison = "none";
            this.state.options.comparison_number = 1;
            this.state.options.comparison_custom_date_from = "";
            this.state.options.comparison_custom_date_to = "";
            this.state.options.comparison_order = "descending";
        }
        if (!this.supportsAccountTypes()) {
            this.state.options.account_type_ids = [];
        }
        for (const key of this.unsupportedOptions()) {
            const value = this.state.options[key];
            if (Array.isArray(value)) {
                this.state.options[key] = [];
            } else if (key === "show_zero") {
                this.state.options[key] = false;
            } else {
                this.state.options[key] = null;
            }
        }
        if (!this.supportsPivot()) {
            this.state.options.horizontal_group_by = null;
        }
        if (!this.supportsAnalyticColumns()) {
            this.state.options.analytic_column_account_ids = [];
            this.state.options.analytic_column_plan_ids = [];
        } else {
            // Saved views can predate report-specific column budgets. Keep
            // explicit accounts first, then plans, within deterministic cap.
            const maximum = this.analyticColumnMax();
            const accounts = normalizePositiveIdList(
                this.state.options.analytic_column_account_ids,
            ).slice(0, maximum);
            const plans = normalizePositiveIdList(
                this.state.options.analytic_column_plan_ids,
            ).slice(0, Math.max(0, maximum - accounts.length));
            this.state.options.analytic_column_account_ids = accounts;
            this.state.options.analytic_column_plan_ids = plans;
        }
        this.state.options.hierarchical_groups =
            this.state.options.hierarchical_groups !== false;
        this.state.options.unfold_all =
            this.state.options.unfold_all === true;
        this.state.options.unfolded_lines = Array.isArray(
            this.state.options.unfolded_lines,
        ) ? [...new Set(this.state.options.unfolded_lines.filter(
            (lineId) => typeof lineId === "string" && lineId.trim(),
        ).map((lineId) => lineId.trim()))] : [];
        this.normalizeComparisonBudgetOptions();
        if (this.state.options.comparison === "none") {
            this.state.options.comparison_number = 1;
        }
        if (this.state.options.comparison === "custom") {
            this.state.options.comparison_number = 1;
        }
        if (this.state.options.comparison !== "custom") {
            this.state.options.comparison_custom_date_from = "";
            this.state.options.comparison_custom_date_to = "";
        }
        if (
            !["ascending", "descending"].includes(
                this.state.options.comparison_order,
            )
            || this.state.options.comparison === "none"
        ) {
            this.state.options.comparison_order = "descending";
        }
    }

    supportsNPeriod() {
        return this.reportCapabilities().includes("nperiod");
    }

    supportsPivot() {
        return this.reportCapabilities().includes("pivot");
    }

    supportsAnalyticColumns() {
        return this.reportCapabilities().includes("analytic_columns")
            && this.supportsOption("analytic_column_account_ids")
            && this.supportsOption("analytic_column_plan_ids");
    }

    analyticColumnMax() {
        // One independent Total accompanies selected groups. Trial Balance
        // needs six measures for every value scope, so eight selected groups
        // would exceed 48 columns even without a comparison period.
        return this.reportCode === "trial_balance" ? 7 : 8;
    }

    comparisonControlsAvailable() {
        return this.supportsComparison() && this.comparisonNumberMax() >= 1;
    }

    get isCashFlow() {
        return this.reportCode === "cash_flow"
            && this.reportCapabilities().includes("recon");
    }

    // Pivot only makes sense when more than one company is in scope; the
    // server branch is guarded by len>1 and silently no-ops otherwise, so
    // we hide the control to avoid a "looks broken" moment.
    get pivotAvailable() {
        return this.supportsPivot()
            && (this.state.options.company_ids || []).length > 1;
    }

    onComparisonNumberChange(event) {
        const v = Number(event.target.value);
        // Clamp to report-specific server budget: cost is linear in N and
        // Trial Balance expands each period into six measures. Non-numeric
        // falls back to one comparison period.
        let n = Number.isInteger(v) ? v : 1;
        if (n < 1) n = 1;
        const maximum = this.comparisonNumberMax();
        if (maximum < 1) {
            this.normalizeComparisonBudgetOptions();
            this.refresh();
            return;
        }
        if (n > maximum) n = maximum;
        this.state.options.comparison_number = n;
        this.refresh();
    }

    comparisonNumberMax() {
        const options = (this.state && this.state.options) || {};
        const analyticGroups = (
            (options.analytic_column_account_ids || []).length
            + (options.analytic_column_plan_ids || []).length
        );
        // Each period owns selected analytic groups plus independent Total.
        // With no selector there is one unsliced value scope. Trial Balance
        // repeats six measures per scope. comparison_number counts priors;
        // current adds one period scope.
        const groupsPerPeriod = analyticGroups ? analyticGroups + 1 : 1;
        const measuresPerScope = this.reportCode === "trial_balance" ? 6 : 1;
        return Math.max(
            0,
            Math.min(
                12,
                Math.floor(
                    48 / (measuresPerScope * groupsPerPeriod),
                ) - 1,
            ),
        );
    }

    normalizeComparisonBudgetOptions() {
        const options = this.state.options;
        const maximum = this.comparisonNumberMax();
        if (maximum < 1) {
            options.comparison = "none";
            options.comparison_number = 1;
            options.comparison_custom_date_from = "";
            options.comparison_custom_date_to = "";
            options.comparison_order = "descending";
            return;
        }
        const requested = Number(options.comparison_number);
        options.comparison_number = Number.isInteger(requested)
            && requested >= 1
            ? Math.min(requested, maximum)
            : 1;
    }

    onAnalyticColumnSelectChange(event, key) {
        const ids = Array.from(event.target.selectedOptions).map(
            (option) => parseInt(option.value, 10),
        ).filter(Number.isFinite);
        const otherKey = key === "analytic_column_account_ids"
            ? "analytic_column_plan_ids" : "analytic_column_account_ids";
        const maximum = this.analyticColumnMax();
        const available = Math.max(
            0, maximum - (this.state.options[otherKey] || []).length,
        );
        if (ids.length > available) {
            this.notification.add(
                sprintf(
                    _t("Select no more than %(maximum)s analytic columns."),
                    { maximum },
                ),
                { type: "warning" },
            );
        }
        this.state.options[key] = ids.slice(0, available);
        this.normalizeComparisonBudgetOptions();
        this.refresh();
    }

    onHorizontalGroupChange(event) {
        const val = event.target.value;
        this.state.options.horizontal_group_by =
            val === "company" ? "company" : null;
        this.refresh();
    }

    onCashFlowMethodChange(event) {
        const val = event.target.value;
        this.state.options.cash_flow_method =
            val === "indirect" ? "indirect" : "direct";
        this.refresh();
    }

    onCashFlowReconciledToggle(event) {
        // Opt-in only: the reconciliation-accurate path walks partials per
        // AR/AP line and is the expensive one, so it never defaults on.
        this.state.options.cash_flow_reconciled = !!event.target.checked;
        this.refresh();
    }

    onCfInterestPaidSectionChange(event) {
        // IAS 7.31 override: '' = follow the company policy field.
        const val = event.target.value;
        this.state.options.cf_interest_paid_section =
            val === "operating" || val === "financing" ? val : "";
        this.refresh();
    }

    onCfDividendsPaidSectionChange(event) {
        // IAS 7.34 override: '' = follow the company policy field.
        const val = event.target.value;
        this.state.options.cf_dividends_paid_section =
            val === "financing" || val === "operating" ? val : "";
        this.refresh();
    }

    // ---- WS3 annotations ----

    // The render payload already carries notes (server _eh_apply_annotations
    // stamps line.meta.annotations for row notes and col.annotations for
    // cell notes), so the viewer never fetches them separately. col===false
    // (or undefined) addresses the whole row; a column def addresses a cell
    // via its expression_label.

    annotationsFor(line, col) {
        if (!line) return [];
        if (col && col.expression_label) {
            const lineCol = (line.columns || []).find(
                (c) => c.expression_label === col.expression_label);
            return (lineCol && lineCol.annotations) || [];
        }
        return (line.meta && line.meta.annotations) || [];
    }

    hasAnnotation(line, col) {
        return this.annotationsFor(line, col).length > 0;
    }

    isAnnotationOpen(line, col) {
        const open = this.state.annotationOpen;
        if (!open || !line) return false;
        const label = (col && col.expression_label) || false;
        return open.lineId === line.id && open.label === label;
    }

    onOpenAnnotation(line, col) {
        if (!line) return;
        const label = (col && col.expression_label) || false;
        if (this.isAnnotationOpen(line, col)) {
            this.state.annotationOpen = null;
            return;
        }
        this.state.annotationOpen = { lineId: line.id, label };
        this.state.annotationDraft = "";
    }

    onCloseAnnotation() {
        this.state.annotationOpen = null;
        this.state.annotationDraft = "";
    }

    onAnnotationDraftInput(event) {
        this.state.annotationDraft = event.target.value;
    }

    async onCreateAnnotation() {
        const open = this.state.annotationOpen;
        const text = (this.state.annotationDraft || "").trim();
        if (!open || !text || !this.state.reportId) return;
        try {
            await this.orm.call(
                "eh.account.dynamic.report",
                "add_annotation",
                [[this.state.reportId], open.lineId, text, open.label || false],
            );
            this.state.annotationDraft = "";
            // A fresh render re-applies notes live (they are deliberately
            // injected after the cache lookup), so the new note appears.
            await this.refresh();
        } catch (exc) {
            // Never block the viewer on a note failure: surface and stay put.
            this.notification.add(
                errorMessage(exc), { type: "danger" },
            );
        }
    }

    async onDeleteAnnotation(annotationId) {
        if (!annotationId || !this.state.reportId) return;
        try {
            await this.orm.call(
                "eh.account.dynamic.report",
                "delete_annotation",
                [[this.state.reportId], annotationId],
            );
            await this.refresh();
        } catch (exc) {
            // Non-managers lack unlink (append-only posture); surface the
            // access error rather than silently swallowing it, but never
            // crash the render.
            this.notification.add(
                errorMessage(exc), { type: "danger" },
            );
        }
    }

    onCurrencyChange(event) {
        const val = event.target.value;
        this.state.options.presentation_currency_id = val ? parseInt(val, 10) : null;
        this.refresh();
    }

    onPostedOnlyToggle(event) {
        this.state.options.posted_only = event.target.checked;
        this.refresh();
    }

    onShowZeroToggle(event) {
        this.state.options.show_zero = event.target.checked;
        this.refresh();
    }

    // ---- WS5 in-table search ----

    onTableSearch(event) {
        // Debounced, purely client-side filter over the already-loaded
        // payload. No RPC, so it stays instant on a huge ledger; the debounce
        // only avoids rebuilding the match set on every keystroke. Scroll is
        // reset to the top so the window does not point past the (shorter)
        // filtered list.
        const term = event.target.value;
        clearTimeout(this._tableSearchTimer);
        this._tableSearchTimer = setTimeout(() => {
            this.state.tableQuery = term || "";
            this.state.scrollTop = 0;
            if (this.bodyRef && this.bodyRef.el) {
                this.bodyRef.el.scrollTop = 0;
            }
        }, SEARCH_DEBOUNCE_MS);
    }

    // The set of payload line ids that should stay on screen for the active
    // query: a line matches when its own name contains the term OR any
    // descendant matches (so a parent stays as context for a matched child)
    // OR any ancestor matches (so the children of a matched group stay).
    // Built once per query change and memoised on (payload, query) so a
    // scroll does not recompute it.
    get tableFilteredIds() {
        const q = (this.state.tableQuery || "").trim().toLowerCase();
        if (!q || !this.state.payload) {
            return null; // null = no active filter; show everything.
        }
        // Memoise: scrolling fires many re-renders but the match set only
        // depends on the payload identity and the query string. The match
        // walk itself lives in the pure (hoot-tested) logic module.
        if (this._filterCache
            && this._filterCache.payload === this.state.payload
            && this._filterCache.children === this.state.childLines
            && this._filterCache.q === q) {
            return this._filterCache.ids;
        }
        const lines = [...(this.state.payload.lines || [])];
        // expand_line children carry no parent_id; link each to its lazy
        // leaf so a matched account keeps its fetched journal items.
        for (const [leafId, entry] of Object.entries(this.state.childLines || {})) {
            for (const child of (entry && entry.lines) || []) {
                lines.push(child.parent_id ? child : { ...child, parent_id: leafId });
            }
        }
        const keep = computeFilterKeepSet(lines, q);
        this._filterCache = {
            payload: this.state.payload,
            children: this.state.childLines,
            q,
            ids: keep,
        };
        return keep;
    }

    // ---- aged-report config (WS2) ----

    get isAged() {
        return this.reportCode === "aged_receivable"
            || this.reportCode === "aged_payable";
    }

    onAgingIntervalChange(event) {
        const v = parseInt(event.target.value, 10);
        this.state.options.aging_interval = Number.isFinite(v) && v > 0 ? v : 30;
        this.refresh();
    }

    onAgingBucketCountChange(event) {
        const v = parseInt(event.target.value, 10);
        this.state.options.aging_bucket_count =
            Number.isFinite(v) && v > 0 ? v : 4;
        this.refresh();
    }

    onAgingBasisChange(event) {
        this.state.options.aging_basis = event.target.value || "maturity";
        this.refresh();
    }

    onReconcileStateChange(event) {
        this.state.options.reconcile_state = event.target.value || "open";
        this.refresh();
    }

    onMultiSelectChange(event, key) {
        const ids = Array.from(event.target.selectedOptions).map(
            (o) => parseInt(o.value, 10),
        );
        this.state.options[key] = ids;
        this.refresh();
    }

    onMultiSelectChangeStr(event, key) {
        const codes = Array.from(event.target.selectedOptions).map((o) => o.value);
        this.state.options[key] = codes;
        this.refresh();
    }

    onClearAllFilters() {
        this.state.options.journal_ids = [];
        this.state.options.partner_ids = [];
        this.state.options.account_ids = [];
        this.state.options.account_type_ids = [];
        this.state.options.analytic_account_ids = [];
        this.state.options.analytic_plan_ids = [];
        this.state.options.analytic_column_account_ids = [];
        this.state.options.analytic_column_plan_ids = [];
        this.state.options.comparison = "none";
        this.state.options.comparison_number = 1;
        this.state.options.comparison_custom_date_from = "";
        this.state.options.comparison_custom_date_to = "";
        this.state.options.comparison_order = "descending";
        this.state.options.horizontal_group_by = null;
        this.state.options.cash_flow_method = "direct";
        this.state.options.cash_flow_reconciled = false;
        this.state.options.cf_interest_paid_section = "";
        this.state.options.cf_dividends_paid_section = "";
        this.state.options.presentation_currency_id = null;
        this.state.options.posted_only = true;
        this.state.options.show_zero = false;
        this.state.options.aging_interval = 30;
        this.state.options.aging_bucket_count = 4;
        this.state.options.aging_basis = "maturity";
        this.state.options.reconcile_state = "open";
        this.state.options.hierarchical_groups = true;
        this.state.options.unfold_all = false;
        this.state.options.unfolded_lines = [];
        this.refresh();
    }

    async openManyToManyPicker(model, optionKey, label) {
        // Multi-select records picker with checkboxes + a Select button.
        // SelectCreateDialog is imported lazily (not at module top level) so
        // a bundle-resolution hiccup can never stop this component from
        // registering; we fall back to the inline multi-select otherwise.
        try {
            const { SelectCreateDialog } = await odoo.loader.modules.get(
                "@web/views/view_dialogs/select_create_dialog");
            this.dialog.add(SelectCreateDialog, {
                title: sprintf(_t("Pick %s"), label),
                resModel: model,
                multiSelect: true,
                domain: [],
                noCreate: false,
                onSelected: (resIds) => {
                    if (!resIds || !resIds.length) {
                        return;
                    }
                    const merged = new Set(
                        [...(this.state.options[optionKey] || []), ...resIds]
                            .map((i) => parseInt(i, 10)),
                    );
                    const ids = Array.from(merged);
                    const bounded = optionKey.startsWith("analytic_column_");
                    const otherKey = optionKey === "analytic_column_account_ids"
                        ? "analytic_column_plan_ids"
                        : "analytic_column_account_ids";
                    const maximum = this.analyticColumnMax();
                    const available = bounded ? Math.max(
                        0, maximum
                            - (this.state.options[otherKey] || []).length,
                    ) : ids.length;
                    if (bounded && ids.length > available) {
                        this.notification.add(
                            sprintf(
                                _t("Select no more than %(maximum)s analytic columns."),
                                { maximum },
                            ),
                            { type: "warning" },
                        );
                    }
                    this.state.options[optionKey] = bounded
                        ? ids.slice(0, available) : ids;
                    if (bounded) this.normalizeComparisonBudgetOptions();
                    this.refresh();
                },
            });
        } catch (e) {
            this.notification.add(
                _t("Use the multi-select list; the full picker is unavailable."),
                { type: "warning" },
            );
        }
    }

    activeFilterCount() {
        const opts = this.state.options;
        let n = 0;
        if (this.supportsOption("journal_ids") && opts.journal_ids.length) n++;
        if (this.supportsOption("partner_ids") && opts.partner_ids.length) n++;
        if (this.supportsOption("account_ids") && opts.account_ids.length) n++;
        if (this.supportsAccountTypes()
                && (opts.account_type_ids || []).length) n++;
        if (this.supportsOption("analytic_account_ids")
                && opts.analytic_account_ids.length) n++;
        if (this.supportsOption("analytic_plan_ids")
                && opts.analytic_plan_ids.length) n++;
        if (this.supportsComparison()
                && opts.comparison && opts.comparison !== "none") n++;
        if (this.supportsComparison()
                && opts.comparison !== "none"
                && opts.comparison_number && opts.comparison_number > 1) n++;
        if (this.supportsComparison()
                && opts.comparison !== "none"
                && opts.comparison_order === "ascending") n++;
        if (this.supportsAnalyticColumns()
                && (opts.analytic_column_account_ids || []).length) n++;
        if (this.supportsAnalyticColumns()
                && (opts.analytic_column_plan_ids || []).length) n++;
        if (opts.horizontal_group_by) n++;
        if (this.isCashFlow && opts.cash_flow_method
                && opts.cash_flow_method !== "direct") n++;
        if (this.isCashFlow && opts.cash_flow_reconciled) n++;
        if (this.isAged) {
            if (opts.reconcile_state && opts.reconcile_state !== "open") n++;
            if (opts.aging_basis && opts.aging_basis !== "maturity") n++;
            if (opts.aging_interval && opts.aging_interval !== 30) n++;
            if (opts.aging_bucket_count && opts.aging_bucket_count !== 4) n++;
        }
        return n;
    }

    activeChips() {
        const chips = [];
        const opts = this.state.options;
        if (this.supportsComparison()
                && opts.comparison && opts.comparison !== "none") {
            const comparisonLabel = opts.comparison === "custom"
                ? sprintf(
                    _t("Compare: %s to %s"),
                    opts.comparison_custom_date_from || "?",
                    opts.comparison_custom_date_to || "?",
                )
                : sprintf(
                    _t("Compare: %s"), opts.comparison.replace("_", " "),
                );
            chips.push({
                label: comparisonLabel,
                key: "comparison",
            });
        }
        if (this.supportsComparison()
                && opts.comparison !== "none"
                && opts.comparison_number && opts.comparison_number > 1) {
            chips.push({
                label: sprintf(
                    _t("Comparison periods: %s"), opts.comparison_number,
                ),
                key: "comparison_number",
            });
        }
        if (this.supportsComparison()
                && opts.comparison !== "none"
                && opts.comparison_order === "ascending") {
            chips.push({
                label: _t("Period order: Oldest first"),
                key: "comparison_order",
            });
        }
        if (opts.horizontal_group_by === "company") {
            chips.push({ label: _t("Layout: By company"), key: "horizontal_group_by" });
        }
        if (this.isCashFlow && opts.cash_flow_method === "indirect") {
            chips.push({ label: _t("Method: Indirect"), key: "cash_flow_method" });
        }
        if (this.isCashFlow && opts.cash_flow_reconciled) {
            chips.push({
                label: _t("Reconciliation-accurate"),
                key: "cash_flow_reconciled",
            });
        }
        const named = [
            ["journal_ids", this.state.choices.journals, _t("Journal")],
            ["partner_ids", this.state.choices.partners, _t("Partner")],
            ["account_ids", this.state.choices.accounts, _t("Account")],
            ["analytic_plan_ids", this.state.choices.analyticPlans, _t("Analytic plan")],
            ["analytic_account_ids", this.state.choices.analyticAccounts, _t("Analytic")],
            ["analytic_column_plan_ids", this.state.choices.analyticPlans, _t("Plan column")],
            ["analytic_column_account_ids", this.state.choices.analyticAccounts, _t("Analytic column")],
        ];
        for (const [key, src, label] of named) {
            if (!this.supportsOption(key)) continue;
            for (const id of opts[key] || []) {
                const rec = src.find((r) => r.id === id);
                chips.push({
                    label: sprintf(
                        _t("%s: %s"), label,
                        rec ? (rec.code ? rec.code + " " : "") + rec.name : id,
                    ),
                    key, id,
                });
            }
        }
        for (const code of this.supportsAccountTypes()
                ? (opts.account_type_ids || []) : []) {
            const t = ACCOUNT_TYPE_CHOICES.find((x) => x.code === code);
            chips.push({
                label: sprintf(_t("Type: %s"), t ? t.label : code),
                key: "account_type_ids", id: code,
            });
        }
        if (this.isAged) {
            if (opts.reconcile_state && opts.reconcile_state !== "open") {
                chips.push({
                    label: _t("Reconcile: all (including reconciled)"),
                    key: "reconcile_state",
                });
            }
            if (opts.aging_basis && opts.aging_basis !== "maturity") {
                chips.push({ label: _t("Basis: invoice date"), key: "aging_basis" });
            }
            if (opts.aging_interval && opts.aging_interval !== 30) {
                chips.push({
                    label: sprintf(_t("Interval: %sd"), opts.aging_interval),
                    key: "aging_interval",
                });
            }
            if (opts.aging_bucket_count && opts.aging_bucket_count !== 4) {
                chips.push({
                    label: sprintf(_t("Buckets: %s"), opts.aging_bucket_count),
                    key: "aging_bucket_count",
                });
            }
        }
        return chips;
    }

    onRemoveChip(chip) {
        const agingDefaults = {
            reconcile_state: "open",
            aging_basis: "maturity",
            aging_interval: 30,
            aging_bucket_count: 4,
        };
        const ws3Defaults = {
            comparison_number: 1,
            comparison_order: "descending",
            horizontal_group_by: null,
            cash_flow_method: "direct",
            cash_flow_reconciled: false,
            cf_interest_paid_section: "",
            cf_dividends_paid_section: "",
        };
        if (chip.key === "comparison") {
            this.state.options.comparison = "none";
            // Dropping the comparison also drops a multi-period request.
            this.state.options.comparison_number = 1;
            this.state.options.comparison_custom_date_from = "";
            this.state.options.comparison_custom_date_to = "";
        } else if (chip.key in ws3Defaults && chip.id === undefined) {
            this.state.options[chip.key] = ws3Defaults[chip.key];
        } else if (chip.key in agingDefaults && chip.id === undefined) {
            this.state.options[chip.key] = agingDefaults[chip.key];
        } else if (chip.id !== undefined) {
            this.state.options[chip.key] = this.state.options[chip.key].filter(
                (x) => x !== chip.id,
            );
        }
        this.refresh();
    }

    // ---- presentation helpers ----

    valueColumnDefs() {
        // Drop the leading label column; the rest are the value columns. The
        // defs already carry expression_label (the sectioned handler stamps
        // 'amount' / 'prior_amount' / 'variance' / 'variance_pct'), which
        // cellClass() reads to tell a variance column from a plain amount.
        if (!this.state.payload) return [];
        return this.state.payload.columns.slice(1);
    }

    columnHeaderRows() {
        if (!this.state.payload) return null;
        return normalizeColumnHeaderRows(
            this.state.payload.columns,
            this.state.payload.column_header_rows,
        );
    }

    formatLineValue(line, valueIndex) {
        const colDef = this.valueColumnDefs()[valueIndex];
        if (!colDef) return "";
        const lineCol = line.columns ? line.columns[valueIndex] : null;
        if (!lineCol) return "";
        return formatCurrency(
            lineCol.value,
            this.state.payload.currency,
            effectiveFigureType(lineCol, colDef),
            ((this.user && this.user.context && this.user.context.lang)
                || "en_US").replace("_", "-"),
        );
    }

    // True when a column expresses a CHANGE (variance / variance %) rather
    // than a raw balance. A change column is coloured by whether the move is
    // favourable, not merely by sign, so a cost reduction reads positive.
    _isComparisonColumn(colDef) {
        const label = colDef && colDef.expression_label;
        return label === "variance" || label === "variance_pct";
    }

    // Whether a higher number is "good" for this row. Income/revenue rows
    // want higher; expense/cost rows want lower. The server may stamp
    // line.meta.higher_is_better (P&L does, keyed on section); when it is
    // absent we return null so the caller falls back to sign-only colouring,
    // which is never worse than the previous all-negatives-red behaviour.
    _higherIsBetter(line) {
        const meta = line && line.meta;
        if (meta && typeof meta.higher_is_better === "boolean") {
            return meta.higher_is_better;
        }
        return null;
    }

    cellClass(line, valueIndex) {
        const colDef = this.valueColumnDefs()[valueIndex];
        const classes = [];
        const lineCol = line.columns ? line.columns[valueIndex] : null;
        const figureType = effectiveFigureType(lineCol, colDef);
        const isNumeric = isNumericFigureType(figureType);
        if (isNumeric) {
            classes.push("text-end");
            // Numeric cells render in mono with tabular figures so columns of
            // numbers line up digit-for-digit like the dashboard KPI tiles.
            classes.push("eh_dr_num");
        }
        const value = lineCol ? lineCol.value : null;
        if (!isNumeric || typeof value !== "number") {
            return classes.join(" ");
        }
        if (this._isComparisonColumn(colDef)) {
            // Semantic good/bad colouring on a change column. The role
            // (good/bad/muted by sign x direction) is computed by the pure,
            // hoot-tested variantCellRole(); higher_is_better=null falls back
            // to sign-only, never worse than today.
            const role = variantCellRole(value, this._higherIsBetter(line));
            const ROLE_CLASS = {
                good: "eh_dr_good", bad: "eh_dr_bad", muted: "eh_dr_muted",
            };
            if (role && ROLE_CLASS[role]) {
                classes.push(ROLE_CLASS[role]);
            }
        } else if (value < 0) {
            // Non-comparison column: keep the plain negative=warm-red rule.
            classes.push("eh_dr_bad");
        }
        return classes.join(" ");
    }

    rowClass(line) {
        const meta = line.meta || {};
        const classes = [];
        if (line.level === 0) classes.push("eh_dr_section_row");
        else classes.push("eh_dr_data_row");
        if (meta.kind === "section_header") classes.push("eh_dr_header");
        if (meta.kind === "section_total") classes.push("eh_dr_total");
        if (meta.kind === "balance_check") classes.push("eh_dr_check");
        if (meta.kind === "net_profit"
            || meta.kind === "net_change"
            || meta.kind === "computed_total") {
            classes.push("eh_dr_computed");
        }
        return classes.join(" ");
    }

    nameStyle(line) {
        const indentEm = (line.level || 0) * 1.5;
        return indentEm ? "padding-left: " + indentEm + "em;" : "";
    }

    isExpanded(line) {
        return this.state.expandedLines.includes(line.id);
    }

    onToggleLine(line) {
        const newlyExpanded = !this.isExpanded(line);
        if (newlyExpanded) {
            this.state.expandedLines = [
                ...this.state.expandedLines, line.id,
            ];
        } else {
            this.state.expandedLines = this.state.expandedLines.filter(
                (id) => id !== line.id,
            );
        }
        // Lazy leaf: fetch the first page of children on first expand only.
        // Collapse keeps the cached children, so a re-expand is instant and
        // does NOT refetch (the windowed builder simply stops splicing them
        // while collapsed). We fetch only when no page is cached yet.
        if (line.lazy && newlyExpanded && !this.state.childLines[line.id]) {
            this.loadChildren(line, 0);
        }
        // Fire-and-forget persistence: any failure leaves the local
        // state intact; the next reload simply falls back to the
        // saved-or-default behaviour. We deliberately do not await
        // so the click feels immediate. Lazy leaves are not persisted as
        // expanded (they always start collapsed on reload), but recording
        // the toggle is harmless because hydrate skips lazy leaves.
        this._persistFold("set_for_user", [line.id, newlyExpanded]);
    }

    // Fold persistence runs through one chain, so the server applies the
    // saves in click order; a failed save never blocks the next one.
    _persistFold(method, args) {
        const code = this.reportCode;
        if (!code) return;
        this._foldSave = (this._foldSave || Promise.resolve())
            .then(() => this.orm.call(
                "eh.account.report.fold.state", method, [code, ...args],
            ))
            .catch(() => {});
    }

    async loadChildren(line, offset) {
        // Fetch one page of an account leaf's journal items via the
        // stateless expand_line RPC. Appends to any already-loaded page so
        // load-more accumulates. Defensive: a failed expand leaves the row
        // expanded-but-empty (or with its prior page), never throwing.
        if (!this.state.reportId || !line || !line.id) return;
        // A refresh resets childLines for the new payload. A response that
        // lands after that belongs to the old options (date range, filters),
        // so it is dropped: written back in, it would be cached and shown on
        // the next expand without a refetch.
        const sequence = this._childLinesSequence;
        const existing = this.state.childLines[line.id] || {
            lines: [], hasMore: false, nextOffset: 0, totalCount: 0,
        };
        // Mark loading so the sentinel can show a spinner; reuse the same
        // object identity is unnecessary because state is reactive.
        this.state.childLines = {
            ...this.state.childLines,
            [line.id]: { ...existing, loading: true },
        };
        try {
            const res = await this.orm.call(
                "eh.account.dynamic.report",
                "expand_line",
                [
                    [this.state.reportId], this.state.options, line.id,
                    offset || 0, null,
                ],
            );
            if (sequence !== this._childLinesSequence) return;
            const fetched = (res && res.child_lines) || [];
            const merged = offset
                ? [...existing.lines, ...fetched]
                : fetched;
            this.state.childLines = {
                ...this.state.childLines,
                [line.id]: {
                    lines: merged,
                    hasMore: !!(res && res.has_more),
                    nextOffset: (res && res.next_offset) || merged.length,
                    totalCount: (res && res.total_count) || merged.length,
                    loading: false,
                },
            };
        } catch (exc) {
            // A failure of a request made obsolete by a refresh is silent.
            if (sequence !== this._childLinesSequence) return;
            // Keep whatever we had; clear the loading flag so the spinner
            // stops and the user can retry by collapsing/re-expanding.
            this.state.childLines = {
                ...this.state.childLines,
                [line.id]: { ...existing, loading: false },
            };
            this.notification.add(
                sprintf(
                    _t("Could not expand this report line: %s"),
                    errorMessage(exc),
                ),
                { type: "danger" },
            );
        }
    }

    onLoadMore(line) {
        const entry = this.state.childLines[line.id];
        if (!entry || entry.loading || !entry.hasMore) return;
        this.loadChildren(line, entry.nextOffset);
    }

    hasHierarchy() {
        return !!(this.state.payload && this.state.payload.lines.some(
            (l) => l.unfoldable,
        ));
    }

    onExpandAll() {
        if (!this.state.payload) return;
        // Expand every group, but NEVER lazy leaves: expanding all lazy
        // leaves would fire one fetch per account and fan out to every
        // journal item (the §2 invariant). Lazy leaves stay collapsed and
        // the user expands the ones they care about individually.
        this.state.expandedLines = this.state.payload.lines
            .filter((l) => l.unfoldable && !l.lazy).map((l) => l.id);
        // Expanded is every group's default, so dropping the saved fold
        // state makes the next visit open the same way. Without this, a
        // group folded once stayed folded on every later visit.
        this._persistFold("reset_for_user", []);
    }

    onCollapseAll() {
        this.state.expandedLines = [];
        // Saved too, so a collapsed report reopens collapsed instead of
        // mixing saved and default folds. __default__ covers the groups a
        // later payload brings (new activity, another period or company).
        if (!this.state.payload) return;
        const states = { __default__: false };
        for (const line of this.state.payload.lines) {
            if (line.unfoldable && !line.lazy) {
                states[line.id] = false;
            }
        }
        this._persistFold("set_many_for_user", [states]);
    }

    visibleLines() {
        if (!this.state.payload) return [];
        // ---- WS5 compose order: filter -> fold-visibility ----
        // Step 1 (filter): when an in-table search is active, restrict the
        // payload to the matched set (matches + their ancestors + the
        // descendants of a matched group) BEFORE the fold walk, so a search
        // result's parents/children stay coherent and the fold walk sees the
        // smaller set. null = no active filter.
        const keepIds = this.tableFilteredIds;
        const sourceLines = keepIds
            ? this.state.payload.lines.filter((l) => keepIds.has(l.id))
            : this.state.payload.lines;
        // Step 2 (fold-visibility): a line is visible only when EVERY
        // ancestor up the parent chain is expanded, not just its direct
        // parent. Checking the direct parent alone let a deeply nested row
        // stay on screen after a grandparent section above its
        // (still-expanded) parent was collapsed: e.g. group -> subgroup ->
        // account, collapse the group and the account leaked through because
        // its parent subgroup was still in the expanded set.
        const userExpanded = new Set(this.state.expandedLines);
        const expanded = new Set(userExpanded);
        const query = keepIds
            ? (this.state.tableQuery || "").trim().toLowerCase()
            : "";
        // While a search is active, a matched row must actually surface even
        // if the user had collapsed its parent group. Temporarily treat every
        // kept (in-filter) line as expanded so the fold walk does not hide a
        // match behind a collapsed ancestor; the user's real expandedLines is
        // untouched, so clearing the search restores the prior fold shape.
        if (keepIds) {
            for (const id of keepIds) {
                expanded.add(id);
            }
        }
        const byId = new Map();
        for (const line of sourceLines) {
            byId.set(line.id, line);
        }
        const result = [];
        for (const line of sourceLines) {
            let parentId = line.parent_id;
            let visible = true;
            const seen = new Set(); // guard against a malformed parent cycle
            while (parentId) {
                if (seen.has(parentId)) break;
                seen.add(parentId);
                const parent = byId.get(parentId);
                // Only a FOLDABLE ancestor gates visibility. A structural
                // section header (unfoldable:false) is always open and must
                // never hide its own account rows - otherwise the accounts
                // under Income / Expenses are invisible with no caret to
                // reveal them. Gate only when a foldable ancestor is collapsed.
                if (parent && parent.unfoldable && !expanded.has(parentId)) {
                    visible = false;
                    break;
                }
                parentId = parent ? parent.parent_id : null;
            }
            if (visible) {
                result.push(line);
                // Splice fetched children directly under an expanded lazy
                // leaf, then a load_more sentinel when more pages remain.
                // (Virtual windowing itself is WS5; this keeps the ordered
                // visible array correct so windowing can slice it later.)
                // The search force expansion above gates ancestors only and
                // must never invent a fetch: forcing open a lazy leaf that
                // was never fetched showed a "Loading..." row under every
                // matched account, spinning forever. Only a leaf the USER
                // expanded gets its full page and a loading or load more
                // row; a leaf the user collapsed still surfaces fetched
                // journal items that match the search themselves.
                const entry = line.lazy ? this.state.childLines[line.id] : null;
                const userOpen = !!line.lazy && userExpanded.has(line.id);
                const searchOpen = !userOpen && !!keepIds && !!entry
                    && !!entry.lines && entry.lines.length > 0;
                if (userOpen || searchOpen) {
                    if (entry && entry.lines && entry.lines.length) {
                        for (const child of entry.lines) {
                            const keep = userOpen
                                ? !keepIds || keepIds.has(child.id)
                                : (child.name || "").toLowerCase().includes(query);
                            if (keep) {
                                result.push(child);
                            }
                        }
                    }
                }
                if (userOpen) {
                    if (entry && (entry.hasMore || entry.loading)) {
                        result.push(this._loadMoreSentinel(line, entry));
                    } else if (!entry || entry.loading === undefined) {
                        // Expanded but no page yet resolved: show a spinner.
                        result.push(this._loadMoreSentinel(line, {
                            loading: true, hasMore: false,
                        }));
                    }
                }
            }
        }
        return result;
    }

    _loadMoreSentinel(line, entry) {
        // A synthetic one-row line the template renders as a "load more" /
        // loading sentinel. level = leaf.level + 1 so it indents under the
        // children. Carries the parent line id so onLoadMore can resolve it.
        return {
            id: "loadmore-" + line.id,
            name: "",
            level: (line.level || 0) + 1,
            parent_id: line.id,
            columns: [],
            unfoldable: false,
            meta: {
                kind: "load_more",
                parent_line_id: line.id,
                loading: !!entry.loading,
                has_more: !!entry.hasMore,
                total_count: entry.totalCount || 0,
            },
        };
    }

    onSentinelClick(line) {
        // Resolve the parent lazy leaf and page the next slice.
        const meta = line.meta || {};
        const parentId = meta.parent_line_id;
        if (!parentId || !this.state.payload) return;
        const parent = this.state.payload.lines.find((l) => l.id === parentId);
        if (parent) {
            this.onLoadMore(parent);
        }
    }

    // ---- WS5 virtual scroll ----
    //
    // Compose order (mandatory): filter -> fold-visibility -> window.
    // visibleLines() above already does filter (step 1) then fold-visibility
    // (step 2). windowedLines() is step 3: slice that ordered visible array
    // to the rows that intersect the viewport (plus overscan) so only ~40
    // rows are ever in the DOM regardless of payload size. The non-rendered
    // height is reproduced by top/bottom spacer rows so the scrollbar still
    // reflects the full list. The slice metadata (full count, start index,
    // spacer heights) is cached per call so the template's spacer getters
    // read the same window the rows came from.

    _computeWindow() {
        const visible = this.visibleLines();
        // Virtual windowing ONLY kicks in above a threshold. Below it, render
        // the full visible list with NO spacer rows, so the table is a single
        // plain <tbody> and the sticky <thead> pins reliably. The spacer-row +
        // async-rerender windowing was desyncing from the native scroll and
        // letting the header detach into the body mid-scroll. The lazy engine
        // keeps initial payloads small (O(accounts/partners), <= ~1500 rows),
        // so every real report renders in this stable, spacer-free path; the
        // window only engages for an exceptionally large expanded view.
        if (visible.length <= VIRTUAL_THRESHOLD) {
            return {
                lines: visible, total: visible.length,
                startIndex: 0, topPad: 0, bottomPad: 0,
            };
        }
        // visibleLines() = filter (step 1) + fold-visibility (step 2); the
        // slice (step 3) is the pure, hoot-tested sliceWindow(). It degrades
        // to the full list when the row-height math is degenerate, so the
        // table never blanks or divides by zero.
        return sliceWindow(visible, {
            rowHeight: ROW_HEIGHT,
            overscan: OVERSCAN,
            scrollTop: this.state.scrollTop || 0,
            viewportPx: this.state.viewportPx || DEFAULT_VIEWPORT_PX,
        });
    }

    windowedLines() {
        // Single source for the rendered slice; cache the whole window object
        // so windowTopPad / windowBottomPad read the matching spacers without
        // recomputing visibleLines() three times per render.
        this._window = this._computeWindow();
        return this._window.lines;
    }

    get windowTopPad() {
        return (this._window && this._window.topPad) || 0;
    }

    get windowBottomPad() {
        return (this._window && this._window.bottomPad) || 0;
    }

    get rowHeight() {
        return ROW_HEIGHT;
    }

    // Count of currently-visible (post-filter, post-fold) payload rows, for
    // the "showing X of Y" meta. Counts payload lines only, so the synthetic
    // load more sentinels and lazily fetched journal items never inflate X
    // past Y. Computed fresh: the meta bar renders before the table body
    // refreshes this._window, so the cached window is one render stale.
    get visibleCount() {
        if (!this.state.payload) return 0;
        const payloadIds = new Set(
            (this.state.payload.lines || []).map((l) => l.id),
        );
        return this.visibleLines().filter((l) => payloadIds.has(l.id)).length;
    }

    // Total payload row count (the Y in "showing X of Y").
    get totalRowCount() {
        return (this.state.payload && this.state.payload.lines
            && this.state.payload.lines.length) || 0;
    }

    // True when the active search yields no rows, so the template shows an
    // empty-state instead of a blank, crashing table.
    get searchHasNoMatch() {
        const q = (this.state.tableQuery || "").trim();
        if (!q) return false;
        const ids = this.tableFilteredIds;
        return !!ids && ids.size === 0;
    }
}

registry.category("actions").add(
    "eh_account_dynamic_report", EhDynamicReportViewer,
);
