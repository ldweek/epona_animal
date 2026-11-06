/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Mutex } from "@web/core/utils/concurrency";
import { ControllerNotFoundError } from "@web/webclient/actions/action_service";
import { router } from "@web/core/browser/router";

/**
 * Home Menu Service
 * Manages navigation state between the Home screen and apps.
 * 
 * State:
 * - hasHomeMenu: true when on Home screen
 * - hasBackgroundAction: true when there's a previous app to return to
 * - toggle(show): Navigate to/from Home screen
 */
export const homeMenuService = {
    dependencies: ["action"],
    start(env) {
        let previousState = null;
        const isHomeScreenUrl = window.location.href.includes('home_screen_dashboard');
        
        const state = {
            hasHomeMenu: isHomeScreenUrl,
            hasBackgroundAction: false,
            toggle,
        };

        const mutex = new Mutex();

        async function toggle(show) {
            return mutex.exec(async () => {
                show = show === undefined ? !state.hasHomeMenu : Boolean(show);
                if (show === state.hasHomeMenu) {
                    return;
                }
                if (show) {
                    // Save the complete router state to restore exact position (including record)
                    previousState = JSON.parse(JSON.stringify(router.current || {}));
                    state.hasBackgroundAction = Boolean(previousState?.actionStack?.length);

                    // Navigate to home screen
                    await env.services.action.doAction("home_screen_dashboard", {
                        clearBreadcrumbs: true,
                    });

                    state.hasHomeMenu = true;
                } else {
                    if (previousState) {
                        // Restore the exact previous state (preserves record position)
                        await env.services.action.loadState(previousState);
                        previousState = null;
                    } else {
                        // Fallback
                        try {
                            await env.services.action.restore();
                        } catch (err) {
                            if (!(err instanceof ControllerNotFoundError)) {
                                throw err;
                            }
                        }
                    }

                    state.hasHomeMenu = false;
                    state.hasBackgroundAction = false;
                }

                env.bus.trigger("HOME-MENU:TOGGLED");
                env.bus.trigger("MENUS:APP-CHANGED");
            });
        }

        env.bus.addEventListener("HOME-MENU:TOGGLED", () => {
            document.body.classList.toggle("o_home_menu_background", state.hasHomeMenu);
        });

        return state;
    },
};

registry.category("services").add("home_menu", homeMenuService);
