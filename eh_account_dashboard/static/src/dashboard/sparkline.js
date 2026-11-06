/** @odoo-module **/

/**
 * Inline SVG area sparkline for a money trend.
 *
 * Drawn as a gradient-filled area under a single smoothed polyline, with
 * a dashed baseline and a glowing end dot marking the latest value. No
 * Chart.js dependency, so the asset bundle stays small and the chart
 * renders on any Odoo deployment. Purely presentational: it expects the
 * data series ready to plot and renders an empty-state when the series
 * has fewer than two points (a single point conveys no trend).
 *
 * Geometry is computed in a fixed 0..VIEW_WIDTH x 0..height viewBox with
 * inner padding so the line and the end dot never touch the edges; the
 * SVG then scales to 100% of its container width.
 */

import { Component } from "@odoo/owl";

const VIEW_WIDTH = 600;
const PAD_X = 6;
const PAD_TOP = 14;
const PAD_BOTTOM = 10;
const DEFAULT_HEIGHT = 116;

export class Sparkline extends Component {
    static template = "eh_account_dashboard.Sparkline";
    static props = {
        // `series` is optional with an empty-array default so the parent
        // can pass `state.snapshot.cash_trend` on the very first render,
        // before the snapshot RPC resolves or on a tenant with no cash
        // journals (server returns []).
        series: {
            type: Array,
            element: { type: Object, shape: { date: String, value: Number } },
            optional: true,
        },
        formatLabel: { type: Function, optional: true },
        accentClass: { type: String, optional: true },
        height: { type: Number, optional: true },
    };
    static defaultProps = {
        accentClass: "eh_dash_spark_default",
        series: [],
        height: DEFAULT_HEIGHT,
    };

    get plottable() {
        return Array.isArray(this.props.series) && this.props.series.length >= 2;
    }

    get height() {
        return this.props.height || DEFAULT_HEIGHT;
    }

    get viewBox() {
        return `0 0 ${VIEW_WIDTH} ${this.height}`;
    }

    /**
     * Map data to SVG-space coordinates. Returns the polyline points,
     * the filled-area path (line down to the baseline and back), the
     * baseline y, the last-point coordinate, and the min/max/first/last
     * values. Returns null when not plottable.
     */
    get geometry() {
        if (!this.plottable) {
            return null;
        }
        const series = this.props.series;
        const h = this.height;
        const values = series.map((p) => p.value);
        const min = Math.min(...values);
        const max = Math.max(...values);
        // Avoid divide-by-zero for a flat series: draw the line through
        // the vertical centre so the value still reads.
        const range = max - min || 1;
        const stepX = (VIEW_WIDTH - 2 * PAD_X) / (series.length - 1);
        const usableY = h - PAD_TOP - PAD_BOTTOM;
        const baseline = h - PAD_BOTTOM;
        const points = series.map((p, i) => {
            const x = PAD_X + i * stepX;
            const y = PAD_TOP + (1 - (p.value - min) / range) * usableY;
            return [x, y];
        });
        const polyline = points.map(([x, y]) => `${x},${y}`).join(" ");
        const first = points[0];
        const last = points[points.length - 1];
        const area =
            `M ${first[0]},${baseline} ` +
            `L ${points.map(([x, y]) => `${x},${y}`).join(" L ")} ` +
            `L ${last[0]},${baseline} Z`;
        return {
            polyline,
            area,
            baseline,
            last: { x: last[0], y: last[1] },
            min,
            max,
            firstValue: series[0].value,
            lastValue: series[series.length - 1].value,
        };
    }

    /**
     * Direction class: mint when the latest value is at or above the
     * first, dim when only slightly below, warm-red when materially
     * below. The threshold is intentionally generous because cash
     * position varies day to day; a 1% dip should not flash red.
     */
    get accentClass() {
        const g = this.geometry;
        if (!g) {
            return this.props.accentClass;
        }
        const delta = g.lastValue - g.firstValue;
        if (delta >= 0) {
            return "eh_dash_spark_up";
        }
        const ratio = Math.abs(delta) / (Math.abs(g.firstValue) || 1);
        if (ratio < 0.05) {
            return "eh_dash_spark_flat";
        }
        return "eh_dash_spark_down";
    }

    formatValue(value) {
        if (this.props.formatLabel) {
            return this.props.formatLabel(value);
        }
        return Number(value || 0).toLocaleString();
    }
}
