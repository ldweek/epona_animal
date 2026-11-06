/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { WebClient } from "@web/webclient/webclient";

patch(WebClient.prototype, {
    async _loadDefaultApp() {
        const hm = this.env.services.home_menu;
        if (hm) {
            await hm.toggle(true);
            return;
        }
        return super._loadDefaultApp();
    },
});
