/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { cookie } from "@web/core/browser/cookie";
import { user } from "@web/core/user";
import { browser } from "@web/core/browser/browser";
import { registry } from "@web/core/registry";
import { session } from "@web/session";

export class ColorSchemeSelector extends Component {
    static template = "home_theme.ColorSchemeSelector";
    static props = {};

    setup() {
        this.state = useState({
            current: user.settings.color_scheme || "light",
        });
    }

    get options() {
        return [
            { value: "light", label: _t("Light"), icon: "fa-sun-o" },
            { value: "dark", label: _t("Dark"), icon: "fa-moon-o" },
            { value: "auto", label: _t("System"), icon: "fa-desktop" },
        ];
    }

    async selectScheme(ev, scheme) {
        ev.stopPropagation();
        ev.preventDefault();
        if (scheme === this.state.current) {
            return;
        }
        this.state.current = scheme;

        await user.setUserSettings("color_scheme", scheme);

        let effectiveScheme;
        if (scheme === "auto") {
            effectiveScheme = browser.matchMedia(
                "(prefers-color-scheme: dark)"
            ).matches
                ? "dark"
                : "light";
        } else {
            effectiveScheme = scheme;
        }

        cookie.set("color_scheme", effectiveScheme);
        browser.location.reload();
    }
}

// The free theme offers the Light / Dark / System switch right in the user
// menu. When the Pro add-on is installed the choice lives inside its
// Theme-settings dialog instead, so this entry hides itself (the Pro's
// session_info sets the ``home_theme_pro`` session flag).
registry.category("user_menuitems").add("home_theme_color_scheme", () => ({
    type: "component",
    contentComponent: ColorSchemeSelector,
    sequence: 27,
    hide: !!session.home_theme_pro,
}));
