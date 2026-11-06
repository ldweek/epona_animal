/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { NavBar } from "@web/webclient/navbar/navbar";

patch(NavBar.prototype, {
    get currentApp() {
        const hm = this.env.services.home_menu;
        if (hm?.hasHomeMenu) {
            return;
        }
        return super.currentApp;
    },
    get currentAppSections() {
        const hm = this.env.services.home_menu;
        if (hm?.hasHomeMenu) {
            return [];
        }
        return super.currentAppSections;
    },
    _openAppMenuSidebar() {
        const hm = this.env.services.home_menu;
        if (hm?.hasHomeMenu) {
            // On the Home screen the burger must offer the apps list right
            // away — never bounce back to the previous app: on phones this
            // button is the only navigation, going back would trap the user.
            this.state.isAllAppsMenuOpened = true;
        }
        return super._openAppMenuSidebar(...arguments);
    },
    onAllAppsBtnClick() {
        const hm = this.env.services.home_menu;
        if (hm) {
            super.onAllAppsBtnClick(...arguments);
            hm.toggle(true);
            this._closeAppMenuSidebar();
            return;
        }
        return super.onAllAppsBtnClick(...arguments);
    },
});
