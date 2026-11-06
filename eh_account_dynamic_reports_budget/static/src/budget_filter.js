/** @odoo-module **/

/**
 * Adds a Budget picker to the dynamic-report options panel for the P&L.
 *
 * Without it, the budget-vs-actual columns this module computes are
 * unreachable: the compute path only fires when options carries a
 * budget_id, and nothing in the base viewer sets it. This patch loads the
 * company's budgets and writes options.budget_id, so the feature the module
 * advertises is actually usable.
 */

import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { EhDynamicReportViewer } from "@eh_account_dynamic_reports/components/dynamic_report/dynamic_report";

export const BUDGET_VARIANCE_EXPRESSIONS = new Set([
    "budget_variance", "budget_variance_pct",
]);

export function reconcileBudgetSelection(selectedId, choices) {
    const selected = Number.parseInt(selectedId, 10);
    const ids = new Set((choices || []).map((choice) => choice.id));
    return Number.isFinite(selected) && ids.has(selected) ? selected : false;
}

patch(EhDynamicReportViewer.prototype, {
    setup() {
        super.setup();
        this.state.budgetChoices = [];
        this.state.budgetSupported = false;
        if (this.state.options && this.state.options.budget_id === undefined) {
            this.state.options.budget_id = false;
        }
        if (this.state.options
                && this.state.options.budget_apportionment === undefined) {
            this.state.options.budget_apportionment = "daily";
        }
        if (this.state.options
                && this.state.options.budget_state_filter === undefined) {
            this.state.options.budget_state_filter = "live";
        }
        this.noBudgetLabel = _t("No budget");
        this.dailyBudgetLabel = _t("Daily overlap");
        this.exactBudgetLabel = _t("Exact budget periods");
        this.liveBudgetLabel = _t("Live budgets");
        this.allBudgetStatesLabel = _t("All lifecycle states");
        this._budgetCompanySignature = null;
    },

    savedViewOptionDefaults() {
        return {
            ...super.savedViewOptionDefaults(),
            budget_id: false,
            budget_apportionment: "daily",
            budget_state_filter: "live",
        };
    },

    async _reloadBudgetChoices() {
        const companyIds = (this.state.options.company_ids || [])
            .map((companyId) => Number.parseInt(companyId, 10))
            .filter(Number.isFinite)
            .sort((left, right) => left - right);
        try {
            const response = await this.orm.call(
                "eh.budget.budget",
                "eh_report_budget_choices",
                [
                    this.reportCode,
                    companyIds,
                    this.state.options.budget_state_filter || "live",
                ],
            );
            this.state.budgetSupported = Boolean(response && response.supported);
            this.state.budgetChoices = (response && response.choices) || [];
        } catch (error) {
            // Optional extension must never stop the base report viewer.
            this.state.budgetSupported = false;
            this.state.budgetChoices = [];
        }
        this.state.options.budget_id = reconcileBudgetSelection(
            this.state.options.budget_id,
            this.state.budgetChoices,
        );
    },

    async refresh() {
        const signature = JSON.stringify({
            companyIds: (this.state.options.company_ids || [])
                .map((companyId) => Number.parseInt(companyId, 10))
                .filter(Number.isFinite)
                .sort((left, right) => left - right),
            stateFilter: this.state.options.budget_state_filter || "live",
        });
        if (signature !== this._budgetCompanySignature) {
            this._budgetCompanySignature = signature;
            await this._reloadBudgetChoices();
        }
        return super.refresh();
    },

    onBudgetChange(ev) {
        const val = ev.target.value;
        this.state.options.budget_id = val ? parseInt(val, 10) : false;
        this.onRefresh();
    },

    onBudgetApportionmentChange(ev) {
        const value = ev.target.value;
        this.state.options.budget_apportionment =
            value === "period_exact" ? "period_exact" : "daily";
        this.onRefresh();
    },

    async onBudgetStateFilterChange(ev) {
        this.state.options.budget_state_filter =
            ev.target.value === "all" ? "all" : "live";
        this._budgetCompanySignature = null;
        await this._reloadBudgetChoices();
        this.onRefresh();
    },

    onAmountCellClick(line, column, valueIndex = null) {
        if (BUDGET_VARIANCE_EXPRESSIONS.has(
            column && column.expression_label,
        )) return;
        return super.onAmountCellClick(line, column, valueIndex);
    },

    isDrillableColumn(line, column) {
        if (BUDGET_VARIANCE_EXPRESSIONS.has(
            column && column.expression_label,
        )) return false;
        return super.isDrillableColumn(line, column);
    },

    _isComparisonColumn(colDef) {
        const label = colDef && colDef.expression_label;
        return BUDGET_VARIANCE_EXPRESSIONS.has(label)
            || super._isComparisonColumn(colDef);
    },
});
