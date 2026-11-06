/** @odoo-module **/

/**
 * Single reactive KPI tile.
 *
 * Renders one card with: caption, primary value (already-formatted by
 * the parent), optional caption secondary text, optional drill-down
 * button. The parent passes a `statusClass` that maps to the suite
 * palette (eh_dash_status_*). This is a pure presentational component;
 * it owns no state and never calls RPC.
 *
 * Why a separate component instead of inlining in the parent template:
 *   - the layout repeats 8+ times, so the duplication is real;
 *   - drill-down semantics are uniform (one button name -> one action);
 *   - keeps the parent template focused on layout, not per-tile markup.
 */

import { Component } from "@odoo/owl";

export class KpiTile extends Component {
    static template = "eh_account_dashboard.KpiTile";
    static props = {
        caption: String,
        value: { type: [String, Number], optional: true },
        secondary: { type: String, optional: true },
        statusClass: { type: String, optional: true },
        icon: { type: String, optional: true },
        drilldownLabel: { type: String, optional: true },
        onDrilldown: { type: Function, optional: true },
        // Optional prior-period comparison. When delta is set, the tile
        // renders a small badge with the absolute change and (when pct
        // is non-null) the percentage change, colour-coded by sign and
        // by whether higher is better.
        delta: { type: [String, Number], optional: true },
        deltaPct: { type: [String, Number], optional: true },
        deltaLabel: { type: String, optional: true },
        higherIsBetter: { type: Boolean, optional: true },
    };
    static defaultProps = {
        statusClass: "eh_dash_status_neutral",
        icon: "fa-circle-o",
        higherIsBetter: true,
    };

    get deltaClass() {
        if (this.props.delta === undefined || this.props.delta === null) {
            return "";
        }
        const v = Number(this.props.delta);
        if (v === 0) {
            return "eh_dash_delta_flat";
        }
        const positive = v > 0;
        const good = positive === this.props.higherIsBetter;
        return good ? "eh_dash_delta_up" : "eh_dash_delta_down";
    }

    get deltaArrow() {
        if (this.props.delta === undefined || this.props.delta === null) {
            return "";
        }
        const v = Number(this.props.delta);
        if (v === 0) {
            return "fa-minus";
        }
        return v > 0 ? "fa-arrow-up" : "fa-arrow-down";
    }
}
