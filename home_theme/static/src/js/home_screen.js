/** @odoo-module **/

import { Component, useState, useRef, onWillStart, onMounted, onPatched, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import { browser } from "@web/core/browser/browser";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { localization } from "@web/core/l10n/localization";

const MAX_SEARCH_RESULTS = 12;
const GROUP_KEY = "home_theme.group_by_category";

/**
 * HomeScreenDashboard - Custom home screen with app grid, search, and drag & drop reordering
 */
// Session cache of the last /web/home_screen payload: coming back to the home
// renders instantly from it while a silent background refresh runs.
const _homeCache = { data: null };

// Memoized wallpaper luminance analysis (recomputed only when it changes).
const _bgContrast = { key: null, value: "" };

// Icon-size preference for the home grid (s / m / l), per browser.
const SIZE_KEY = "ht_grid_size";

// Accent-insensitive text folding for the home search.
function foldText(str) {
    return (str || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
}

export class HomeScreenDashboard extends Component {
    static template = "HomeTheme.Dashboard";
    static props = { ...standardActionServiceProps };

    setup() {
        this.action = useService("action");
        this.menu = useService("menu");
        this.homeMenu = useService("home_menu");
        this.rootRef = useRef("root");
        this.searchInputRef = useRef("searchInput");

        this.state = useState({
            apps: [],
            userName: '',
            companyName: '',
            backgroundImage: null,
            backgroundUrl: null,
            backgroundMimetype: 'image/png',
            searchQuery: '',
            searchResults: [],
            searchableMenus: [],
            showSearchResults: false,
            showNoResults: false,
            selectedResultIndex: -1,
            groupByCategory: browser.localStorage.getItem(GROUP_KEY) === "1",
            gridSize: browser.localStorage.getItem(SIZE_KEY) || "m",
            kbSelectedId: null,
            expandedCategories: {},
            bgContrast: '',          // '', 'dark' or 'light' (smart wallpaper contrast)
        });

        this.isDragging = false;
        this._onClickOutside = this._onClickOutside.bind(this);
        this._onGlobalKeydown = this._onGlobalKeydown.bind(this);
        // Extension hook: lets an add-on (Pro Theme Settings dialog) change
        // the icon size live while the home is mounted.
        this._onGridSizeEvent = (ev) => {
            if (['s', 'm', 'l'].includes(ev.detail)) {
                this.state.gridSize = ev.detail;
            }
        };

        onWillStart(async () => {
            await this.loadHomeScreenData();
        });

        onMounted(() => {
            this.homeMenu.hasHomeMenu = true;
            this.env.bus.trigger("HOME-MENU:TOGGLED");
            this.env.bus.trigger("MENUS:APP-CHANGED");
            this.applyStyles();
            this.analyzeBackgroundContrast();
            if (!this.state.groupByCategory) {
                this.initDragAndDrop();
            } else {
                this.initFavoriteDrag();
            }
            document.addEventListener('click', this._onClickOutside);
            document.addEventListener('keydown', this._onGlobalKeydown);
            window.addEventListener('ht:grid-size', this._onGridSizeEvent);
        });

        // Re-attach drag & drop after re-renders (e.g. toggling group mode).
        // Flat mode = reorder + favorite drag; grouped mode = favorite drag only.
        // The dataset guards keep it from double-binding.
        onPatched(() => {
            if (!this.state.groupByCategory) {
                this.initDragAndDrop();
            } else {
                this.initFavoriteDrag();
            }
        });

        onWillUnmount(() => {
            this.homeMenu.hasHomeMenu = false;
            this.homeMenu.hasBackgroundAction = false;
            this.env.bus.trigger("HOME-MENU:TOGGLED");
            this.env.bus.trigger("MENUS:APP-CHANGED");
            document.removeEventListener('click', this._onClickOutside);
            document.removeEventListener('keydown', this._onGlobalKeydown);
            window.removeEventListener('ht:grid-size', this._onGridSizeEvent);
        });
    }

    async loadHomeScreenData() {
        // Stale-while-revalidate: render instantly from the session cache,
        // then refresh silently in the background.
        if (_homeCache.data) {
            this._applyHomeData(_homeCache.data);
            this._refreshHomeData();
            return;
        }
        await this._refreshHomeData();
    }

    async _refreshHomeData() {
        try {
            const data = await rpc("/web/home_screen", {});
            _homeCache.data = data;
            this._applyHomeData(data);
        } catch (error) {
            if (!_homeCache.data) {
                this.state.apps = [];
                this.state.searchableMenus = [];
            }
        }
    }

    _applyHomeData(data) {
        // Rehydrate the app icon onto each searchable menu row: the server
        // sends the heavy base64 icon once per app, not once per menu.
        const appById = new Map((data.apps || []).map((a) => [a.id, a]));
        for (const m of data.searchable_menus || []) {
            if (!m.web_icon_data) {
                const app = appById.get(m.root_app_id);
                if (app && app.web_icon_data) {
                    m.web_icon_data = app.web_icon_data;
                    m.web_icon_data_mimetype = app.web_icon_data_mimetype || "image/png";
                }
            }
        }
        this.state.apps = data.apps || [];
        this.state.userName = data.user_name || '';
        this.state.companyName = data.company_name || '';
        this.state.backgroundUrl = data.background_url || null;
        this.state.backgroundImage = data.background_image || null;
        this.state.backgroundMimetype = data.background_mimetype || 'image/png';
        this.state.searchableMenus = data.searchable_menus || [];
        // A background refresh can land after mount: re-apply the wallpaper
        // and its contrast flag in that case.
        if (this.rootRef.el) {
            this.applyStyles();
            this.analyzeBackgroundContrast();
        }
    }

    // --- Search methods ---

    onSearchInput(ev) {
        const query = ev.target.value.trim();
        this.state.searchQuery = query;
        this.state.selectedResultIndex = -1;

        if (query.length < 1) {
            this.state.searchResults = [];
            this.state.showSearchResults = false;
            return;
        }

        const lowerQuery = foldText(query);
        const terms = lowerQuery.split(/\s+/);

        const results = this.state.searchableMenus.filter((menu) => {
            const haystack = foldText(menu.full_path || menu.name);
            return terms.every((term) => haystack.includes(term));
        });

        // Sort: exact name match first, then by path length (shorter = more relevant)
        results.sort((a, b) => {
            const aName = foldText(a.name);
            const bName = foldText(b.name);
            const aExact = aName === lowerQuery ? 0 : 1;
            const bExact = bName === lowerQuery ? 0 : 1;
            if (aExact !== bExact) return aExact - bExact;
            return (a.full_path || '').length - (b.full_path || '').length;
        });

        this.state.searchResults = results.slice(0, MAX_SEARCH_RESULTS);
        this.state.showSearchResults = results.length > 0;
        this.state.showNoResults = results.length === 0;
    }

    // Clear the search the moment focus leaves the bar. Result rows keep the
    // input focused (they preventDefault on mousedown), so clicking a result
    // does NOT blur — its click navigates normally. Any other outside click
    // blurs the input and clears instantly.
    onSearchBlur() {
        if (this.state.searchQuery) {
            this.clearSearch();
        }
    }

    onSearchKeydown(ev) {
        if (!this.state.showSearchResults) return;
        const results = this.state.searchResults;

        if (ev.key === 'ArrowDown') {
            ev.preventDefault();
            this.state.selectedResultIndex = Math.min(
                this.state.selectedResultIndex + 1,
                results.length - 1
            );
            this._scrollSelectedIntoView();
        } else if (ev.key === 'ArrowUp') {
            ev.preventDefault();
            this.state.selectedResultIndex = Math.max(
                this.state.selectedResultIndex - 1,
                0
            );
            this._scrollSelectedIntoView();
        } else if (ev.key === 'Enter') {
            ev.preventDefault();
            const idx = this.state.selectedResultIndex;
            if (idx >= 0 && idx < results.length) {
                this.onSearchResultClick(results[idx]);
            } else if (results.length > 0) {
                this.onSearchResultClick(results[0]);
            }
        } else if (ev.key === 'Escape') {
            this.clearSearch();
        }
    }

    // Keep the keyboard-selected result visible: scroll it into view within the
    // results dropdown after the re-render (so arrowing past the last VISIBLE
    // row follows the scroll instead of stopping).
    _scrollSelectedIntoView() {
        requestAnimationFrame(() => {
            const item = document.querySelector(
                ".o_home_search_result_item.o_selected"
            );
            if (!item) {
                return;
            }
            const container = item.closest(".o_home_search_results");
            if (!container) {
                return;
            }
            // Scroll the container by the MINIMUM needed so the selected row
            // sits flush against the top/bottom edge — one row per keypress,
            // never a page jump (which scrollIntoView could cause).
            const itemTop = item.offsetTop;
            const itemBottom = itemTop + item.offsetHeight;
            const viewTop = container.scrollTop;
            const viewBottom = viewTop + container.clientHeight;
            if (itemTop < viewTop) {
                container.scrollTop = itemTop;
            } else if (itemBottom > viewBottom) {
                container.scrollTop = itemBottom - container.clientHeight;
            }
        });
    }

    async onSearchResultClick(menuItem) {
        if (!menuItem) return;
        this.clearSearch();
        try {
            await this.menu.selectMenu(menuItem.root_app_id);
            if (menuItem.action_id) {
                await this.action.doAction(menuItem.action_id, {
                    clearBreadcrumbs: true,
                });
            }
        } catch (error) {
            console.error("[HomeScreen] Error navigating to search result:", menuItem.name, error);
        }
    }

    clearSearch() {
        this.state.searchQuery = '';
        this.state.searchResults = [];
        this.state.showSearchResults = false;
        this.state.showNoResults = false;
        this.state.selectedResultIndex = -1;
        if (this.searchInputRef.el) {
            this.searchInputRef.el.value = '';
        }
    }

    _onClickOutside(ev) {
        const searchContainer = document.querySelector('.o_home_search_container');
        if (searchContainer && !searchContainer.contains(ev.target)) {
            this.state.showSearchResults = false;
            this.state.showNoResults = false;
        }
    }

    onSearchFocus() {
        if (this.state.searchQuery.length >= 1) {
            if (this.state.searchResults.length > 0) {
                this.state.showSearchResults = true;
            } else {
                this.state.showNoResults = true;
            }
        }
    }

    // --- Greeting helpers ---

    get greeting() {
        const hour = new Date().getHours();
        if (hour < 12) return _t("Good morning");
        if (hour < 18) return _t("Good afternoon");
        return _t("Good evening");
    }

    get currentDate() {
        const localeCode = (localization.code || "en-US").replace("_", "-");
        const options = { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' };
        const formatted = new Intl.DateTimeFormat(localeCode, options).format(new Date());
        return formatted.charAt(0).toUpperCase() + formatted.slice(1);
    }

    _onGlobalKeydown(ev) {
        if ((ev.ctrlKey || ev.metaKey) && ev.key === 'k') {
            ev.preventDefault();
            if (this.searchInputRef.el) {
                this.searchInputRef.el.focus();
            }
            return;
        }
        this._handleGridKeys(ev);
    }

    /** Apps reachable by keyboard, in visual order (grouped mode flattens the
     *  expanded categories). */
    _kbApps() {
        if (!this.state.groupByCategory) {
            return this.visibleApps;
        }
        const apps = [];
        for (const group of this.appGroups) {
            if (!this.isCategoryCollapsed(group.key)) {
                apps.push(...group.apps);
            }
        }
        return apps;
    }

    /** Cards per row, measured from the live grid. */
    _kbColumns() {
        const grid = this.rootRef.el && this.rootRef.el.querySelector('.o_home_apps_grid');
        const cards = grid ? grid.querySelectorAll('.o_home_app_card') : [];
        if (!cards.length) {
            return 6;
        }
        const firstTop = cards[0].offsetTop;
        let cols = 0;
        for (const card of cards) {
            if (card.offsetTop !== firstTop) {
                break;
            }
            cols++;
        }
        return cols || 6;
    }

    /** Keyboard navigation across the app grid. Carefully scoped so it never
     *  fights an input, a dialog or a Pro overlay (command palette, folder
     *  pop, badge popover, sticky-note editing). */
    _handleGridKeys(ev) {
        const NAV_KEYS = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Enter', 'Escape'];
        const isPrintable = ev.key.length === 1 && !ev.ctrlKey && !ev.metaKey && !ev.altKey;
        if (!NAV_KEYS.includes(ev.key) && !isPrintable) {
            return;
        }
        const target = ev.target;
        if (target && target.closest &&
            target.closest('input, textarea, select, [contenteditable="true"]')) {
            return;
        }
        if (document.querySelector(
            '.modal.show, .o_dialog, .o_home_folder_overlay, .o_ht_badge_pop, [class*="o_ht_cmdk"]')) {
            return;
        }
        if (this.state.showSearchResults) {
            return;
        }

        if (isPrintable) {
            // Start typing anywhere on the home: the search box catches it.
            if (ev.key !== ' ' && this.searchInputRef.el) {
                this.searchInputRef.el.focus();
                this.state.kbSelectedId = null;
            }
            return;
        }

        if (ev.key === 'Escape') {
            this.state.kbSelectedId = null;
            return;
        }

        const apps = this._kbApps();
        if (!apps.length) {
            return;
        }
        const idx = apps.findIndex((app) => app.id === this.state.kbSelectedId);

        if (ev.key === 'Enter') {
            if (ev.shiftKey || ev.ctrlKey || ev.metaKey || ev.altKey) {
                // Extension hook: Pro turns modified Enter into power combos
                // (new tab / new record) on the selected tile.
                if (idx >= 0 && typeof this._kbEnterCombo === "function") {
                    this._kbEnterCombo(ev, apps[idx]);
                }
                return;
            }
            if (idx >= 0) {
                ev.preventDefault();
                this.onAppClick(apps[idx], ev);
            }
            return;
        }

        ev.preventDefault();
        const cols = this._kbColumns();
        let next = 0;
        if (idx >= 0) {
            if (ev.key === 'ArrowRight') {
                next = Math.min(apps.length - 1, idx + 1);
            } else if (ev.key === 'ArrowLeft') {
                next = Math.max(0, idx - 1);
            } else if (ev.key === 'ArrowDown') {
                next = Math.min(apps.length - 1, idx + cols);
            } else {
                next = Math.max(0, idx - cols);
            }
        }
        this.state.kbSelectedId = apps[next].id;
        const card = this.rootRef.el && this.rootRef.el.querySelector(
            `.o_home_app_card[data-app-id="${apps[next].id}"]`);
        if (card) {
            card.scrollIntoView({ block: 'nearest' });
        }
    }

    /** Cycle the home grid icon size: S -> M -> L (stored per browser). */
    cycleGridSize() {
        const order = ['s', 'm', 'l'];
        const next = order[(order.indexOf(this.state.gridSize) + 1) % order.length];
        this.state.gridSize = next;
        try {
            browser.localStorage.setItem(SIZE_KEY, next);
        } catch {
            // private mode: preference just won't persist
        }
    }

    // --- Existing methods ---

    applyStyles() {
        const dashboard = this.rootRef.el;
        if (!dashboard || (!this.state.backgroundUrl && !this.state.backgroundImage)) return;

        const mimetype = this.state.backgroundMimetype || 'image/png';
        dashboard.style.backgroundImage = this.state.backgroundUrl
            ? `url('${this.state.backgroundUrl}')`
            : `url('data:${mimetype};base64,${this.state.backgroundImage}')`;
        dashboard.style.backgroundSize = 'cover';
        dashboard.style.backgroundPosition = 'center';
        dashboard.style.backgroundRepeat = 'no-repeat';
        // Anchor the image to the viewport, not the (variable-height) content,
        // so collapsing/expanding category groups never resizes the background.
        dashboard.style.backgroundAttachment = 'fixed';
    }

    /**
     * Smart wallpaper contrast: sample the background image's average luminance
     * and flag the dashboard so the CSS switches text/icons to light or dark
     * for readability, regardless of the theme.
     */
    async analyzeBackgroundContrast() {
        const source = this.state.backgroundUrl
            || (this.state.backgroundImage
                ? `data:${this.state.backgroundMimetype || 'image/png'};base64,${this.state.backgroundImage}`
                : "");
        if (!source) {
            this.state.bgContrast = '';
            return;
        }
        // Memoized: decoding + sampling the wallpaper is not free, and the
        // image only changes when the admin picks a new one.
        if (_bgContrast.key === source) {
            this.state.bgContrast = _bgContrast.value;
            return;
        }

        try {
            const img = new Image();
            img.src = source;
            await img.decode();

            const canvas = document.createElement('canvas');
            const w = (canvas.width = 48);
            const h = (canvas.height = 48);
            const ctx = canvas.getContext('2d', { willReadFrequently: true });
            ctx.drawImage(img, 0, 0, w, h);

            const { data } = ctx.getImageData(0, 0, w, h);
            let total = 0;
            for (let i = 0; i < data.length; i += 4) {
                total += 0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2];
            }
            const avg = total / (data.length / 4);     // 0 (black) .. 255 (white)
            // Bind via state so OWL keeps the class across re-renders.
            this.state.bgContrast = avg < 140 ? 'dark' : 'light';
            _bgContrast.key = source;
            _bgContrast.value = this.state.bgContrast;
        } catch {
            // Analysis failed (e.g. decode error) -> keep the theme defaults.
            this.state.bgContrast = '';
        }
    }

    initDragAndDrop() {
        const container = this.rootRef.el;
        if (!container) return;

        const grid = container.querySelector('.o_home_apps_grid');
        if (!grid) return;

        const appCards = grid.querySelectorAll('.o_home_app_card');
        if (!appCards || appCards.length === 0) return;

        // Dataset guards keep this idempotent across re-renders/toggles.
        appCards.forEach((card) => {
            if (card.dataset.htDrag) return;
            card.dataset.htDrag = '1';
            card.setAttribute('draggable', 'true');

            card.addEventListener('dragstart', (e) => {
                this.isDragging = true;
                this._draggedElement = card;
                card.classList.add('o_dragging');
                // copyMove so both the grid (move = reorder) and the favorites
                // island (copy) are valid drop effects — otherwise the browser
                // shows a "not-allowed" cursor over the island.
                e.dataTransfer.effectAllowed = 'copyMove';
                e.dataTransfer.setData('text/html', '');
                // App id so a drop target (e.g. the favorites island) can read it.
                e.dataTransfer.setData('application/x-home-app', card.dataset.appId || '');
                this._setDragImage(e, card);
            });

            card.addEventListener('dragend', () => {
                card.classList.remove('o_dragging');
                this._draggedElement = null;
                setTimeout(() => {
                    this.isDragging = false;
                    this.saveAppOrder();
                }, 100);
            });
        });

        if (grid.dataset.htDrag) return;
        grid.dataset.htDrag = '1';

        grid.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            if (!this._draggedElement) {
                return;
            }
            // dragover fires far faster than the display refreshes, and the
            // slot computation reads layout: allow one pass per frame.
            if (this._dragFrameGate) {
                return;
            }
            this._dragFrameGate = true;
            requestAnimationFrame(() => (this._dragFrameGate = false));
            const afterElement = this.getDragAfterElement(grid, e.clientX, e.clientY) || null;
            // Only move (and animate) when the drop slot actually changes, so the
            // other cards don't reshuffle on every dragover tick.
            if (this._draggedElement.nextElementSibling === afterElement) {
                return;
            }
            this._flipReorder(grid, () => {
                if (afterElement == null) {
                    grid.appendChild(this._draggedElement);
                } else {
                    grid.insertBefore(this._draggedElement, afterElement);
                }
            });
        });

        grid.addEventListener('drop', (e) => e.preventDefault());
    }

    // --- App grouping (by module category) ---

    // Single source of truth for the apps rendered on the home (both the flat
    // grid and the grouped view go through this). The Pro add-on overrides it to
    // hide apps already pinned to the favorites dock — and restore them when
    // unpinned. Free shows every app.
    get visibleApps() {
        return this.state.apps;
    }

    get appGroups() {
        const groups = [];
        const byKey = {};
        for (const app of this.visibleApps) {
            // `key` is the stable original category (used for collapse state and,
            // in Pro, for rename/reorder); `name` is the displayed label.
            const key = app.category || 'Other';
            if (!byKey[key]) {
                byKey[key] = { key, name: key, apps: [] };
                groups.push(byKey[key]);
            }
            byKey[key].apps.push(app);
        }
        return groups;
    }

    toggleGroupMode() {
        this.state.groupByCategory = !this.state.groupByCategory;
        browser.localStorage.setItem(GROUP_KEY, this.state.groupByCategory ? "1" : "0");
    }

    toggleCategory(name) {
        this.state.expandedCategories[name] = !this.state.expandedCategories[name];
    }

    // Collapsed by default; a category is only open once the user expands it.
    isCategoryCollapsed(name) {
        return !this.state.expandedCategories[name];
    }

    // Make app cards draggable (carrying their app id) even in grouped mode,
    // so they can be dropped onto the favorites island. Reordering is flat-only.
    initFavoriteDrag() {
        const container = this.rootRef.el;
        if (!container) return;
        container.querySelectorAll('.o_home_app_card').forEach((card) => {
            if (card.dataset.htFavDrag || card.dataset.htDrag) return;
            card.dataset.htFavDrag = '1';
            card.setAttribute('draggable', 'true');
            card.addEventListener('dragstart', (e) => {
                e.dataTransfer.effectAllowed = 'copyMove';
                e.dataTransfer.setData('application/x-home-app', card.dataset.appId || '');
                this._setDragImage(e, card);
            });
        });
    }

    /** Use a clean clone of the card as the drag image — a crisp "card" that
     *  follows the cursor (like the dock's clone), instead of the browser's
     *  translucent ghost. The original stays put as a faint placeholder marking
     *  the drop slot (see .o_dragging). */
    _setDragImage(e, card) {
        const rect = card.getBoundingClientRect();
        const clone = card.cloneNode(true);
        clone.classList.add('o_home_app_card_dragclone');
        clone.classList.remove('o_dragging');
        clone.style.cssText =
            'position:fixed;top:-9999px;left:-9999px;margin:0;pointer-events:none;' +
            `width:${rect.width}px;height:${rect.height}px;`;
        document.body.appendChild(clone);
        e.dataTransfer.setDragImage(clone, rect.width / 2, rect.height / 2);
        setTimeout(() => clone.remove(), 0);
    }

    /** FLIP: smoothly animate the OTHER cards to their new positions when the
     *  dragged card is re-inserted, so the grid opens a clean gap instead of the
     *  cards snapping around. offsetLeft/Top ignore transforms, so overlapping
     *  animations stay stable. */
    _flipReorder(grid, mutate) {
        const items = [...grid.children];
        const first = new Map();
        for (const el of items) {
            first.set(el, { left: el.offsetLeft, top: el.offsetTop });
        }
        mutate();
        const moved = [];
        for (const el of items) {
            if (el === this._draggedElement) {
                continue;
            }
            const f = first.get(el);
            const dx = f.left - el.offsetLeft;
            const dy = f.top - el.offsetTop;
            if (!dx && !dy) {
                continue;
            }
            // Invert: jump each card back to where it was (no transition yet).
            el.style.transition = 'none';
            el.style.transform = `translate(${dx}px, ${dy}px)`;
            moved.push(el);
        }
        if (!moved.length) {
            return;
        }
        // A single forced reflow commits those inverted positions; then play —
        // far more reliable (no dropped transitions) and smoother than a rAF.
        void grid.offsetWidth;
        for (const el of moved) {
            el.style.transition = 'transform 0.28s cubic-bezier(0.4, 0, 0.2, 1)';
            el.style.transform = '';
        }
    }

    getDragAfterElement(container, x, y) {
        // Insert before the first card the cursor is "before" in reading order:
        // a row above it, or the same row left of its centre. So the swap happens
        // as you move OVER a card (cross its midpoint), reserving that slot for
        // the dragged app — not only when you land exactly between two cards.
        const cards = [...container.querySelectorAll('.o_home_app_card:not(.o_dragging)')];
        for (const card of cards) {
            const box = card.getBoundingClientRect();
            if (y < box.top) {
                return card; // cursor is in a row above this card
            }
            if (y <= box.bottom && x < box.left + box.width / 2) {
                return card; // same row, left of this card's centre
            }
        }
        return null; // after every card → append at the end
    }

    async saveAppOrder() {
        try {
            const container = this.rootRef.el;
            if (!container) return;

            const appCards = container.querySelectorAll('.o_home_app_card');
            if (!appCards || appCards.length === 0) return;

            const appIds = [];
            appCards.forEach((card) => {
                const appId = parseInt(card.dataset.appId);
                if (appId) appIds.push(appId);
            });

            const result = await rpc('/web/home_screen/save_order', { app_ids: appIds });
            if (result.success) {
                const appsById = new Map(this.state.apps.map((app) => [app.id, app]));
                const newAppsOrder = appIds.map((id) => appsById.get(id)).filter(Boolean);
                this.state.apps = newAppsOrder;
            }
        } catch (error) {
            // Silent fail - order will be restored on next load
        }
    }

    async onAppClick(app, ev) {
        if (this.isDragging) {
            ev.preventDefault();
            ev.stopPropagation();
            return;
        }
        if (!app || !app.id) return;

        try {
            const menu = this.menu.getMenu(app.id);
            if (menu && menu.actionID) {
                await this.menu.selectMenu(menu);
            } else if (app.action_id) {
                await this.action.doAction(app.action_id, { clearBreadcrumbs: true });
            } else {
                console.warn(`[HomeScreen] App "${app.name}" (id=${app.id}) has no actionID`);
            }
        } catch (error) {
            console.error("[HomeScreen] Error navigating to app:", app.name, error);
        }
    }
}

// Breadcrumb label of the home client action: without it, any record opened
// from the home keeps a nameless ("Unnamed") parent crumb in the trail.
HomeScreenDashboard.displayName = _t("Home");

registry.category("actions").add("home_screen_dashboard", HomeScreenDashboard);
