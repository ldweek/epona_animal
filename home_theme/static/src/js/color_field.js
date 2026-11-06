/** @odoo-module **/

import { Component, useState, useRef, useExternalListener } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { _t } from "@web/core/l10n/translation";

// A friendly, curated palette so users rarely need the OS colour dialog.
const PRESETS = [
    "#243742", "#37474F", "#263238", "#0B3D91", "#1565C0", "#1B5E20",
    "#2E7D32", "#5D8DA8", "#17A2B8", "#28A745", "#F0AD4E", "#E64A19",
    "#DC3545", "#7B1FA2", "#4A148C", "#546E7A", "#FFFFFF", "#000000",
];

/**
 * Nicer colour field: a swatch + hex button that opens an inline popover with a
 * curated palette, a hex input and a fine-tune picker - instead of jumping
 * straight to the clunky OS colour dialog the native ``color`` widget uses.
 */
export class HtColorField extends Component {
    static template = "home_theme.HtColorField";
    static props = { ...standardFieldProps };

    setup() {
        this.state = useState({ open: false });
        this.root = useRef("root");
        useExternalListener(window, "mousedown", (ev) => {
            if (this.state.open && this.root.el && !this.root.el.contains(ev.target)) {
                this.state.open = false;
            }
        });
    }

    get presets() {
        return PRESETS;
    }

    get value() {
        return this.props.record.data[this.props.name] || "";
    }

    get displayValue() {
        return this.value || _t("Pick a colour");
    }

    isSelected(c) {
        return c.toLowerCase() === this.value.toLowerCase();
    }

    async update(val) {
        await this.props.record.update({ [this.props.name]: val });
    }

    toggle() {
        if (!this.props.readonly) {
            this.state.open = !this.state.open;
        }
    }

    onPreset(c) {
        this.update(c);
        this.state.open = false;
    }

    onHex(ev) {
        let v = (ev.target.value || "").trim();
        if (v && !v.startsWith("#")) {
            v = "#" + v;
        }
        this.update(v);
    }

    onNative(ev) {
        this.update(ev.target.value);
    }
}

export const htColorField = {
    component: HtColorField,
    displayName: _t("Color"),
    supportedTypes: ["char"],
};

registry.category("fields").add("ht_color", htColorField);
// Make it the default colour picker everywhere: override the native "color"
// widget so native fields (and any add-on using widget="color") also get the
// friendlier palette popover instead of the OS colour dialog.
registry.category("fields").add("color", htColorField, { force: true });
