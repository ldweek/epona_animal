/** @odoo-module **/
// ============================================================================
// ERP Heritage
// Copyright (C) 2026 (https://www.erpheritage.com.au/)
// ============================================================================
//
// EhCellEllipsis - a tiny presentational cell wrapper for long text.
//
// A long account / partner name silently truncates today (the table cell is
// nowrap + overflow:hidden). This component makes the truncation honest: when
// the text exceeds a threshold it renders the clipped text plus a small "..."
// affordance; clicking it opens a popover showing the full text with a Copy
// button. Below the threshold it renders the text verbatim with zero chrome,
// so the common case stays cheap (the table runs cellClass/ellipsis per
// VISIBLE row only under the WS5 virtual window).

import { Component, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";
import { usePopover } from "@web/core/popover/popover_hook";

// Popover body: full text + a Copy action. Kept in the same module so the
// whole affordance is one file pair (js + xml template).
export class EhCellEllipsisPopover extends Component {
    static template = "eh_account_dynamic_reports.CellEllipsisPopover";
    static props = {
        text: { type: String },
        theme: { type: String, optional: true },
        close: { type: Function, optional: true },
        onCopy: { type: Function, optional: true },
    };

    onCopyClick() {
        if (this.props.onCopy) {
            this.props.onCopy(this.props.text);
        }
    }
}

export class EhCellEllipsis extends Component {
    static template = "eh_account_dynamic_reports.CellEllipsis";
    static components = { EhCellEllipsisPopover };
    static props = {
        text: { type: String, optional: true },
        maxChars: { type: Number, optional: true },
        theme: { type: String, optional: true },
    };
    static defaultProps = {
        text: "",
        maxChars: 48,
        theme: "dark",
    };

    setup() {
        this.notification = useService("notification");
        this.targetRef = useRef("target");
        this.state = useState({});
        // usePopover wires open/close lifecycle and positioning for us; the
        // popover is closed automatically on outside click / scroll.
        const options = {
            position: "bottom",
            popoverClass: "eh_dr_ellipsis_popover",
        };
        this.popover = usePopover(EhCellEllipsisPopover, options);
    }

    get text() {
        return this.props.text || "";
    }

    get isLong() {
        return this.text.length > (this.props.maxChars || 48);
    }

    get truncated() {
        if (!this.isLong) {
            return this.text;
        }
        // Reserve one char for the ellipsis glyph rendered separately.
        return this.text.slice(0, (this.props.maxChars || 48) - 1);
    }

    onReveal() {
        if (!this.targetRef.el) {
            return;
        }
        if (this.popover.isOpen) {
            this.popover.close();
            return;
        }
        this.popover.open(this.targetRef.el, {
            text: this.text,
            theme: this.props.theme === "light" ? "light" : "dark",
            onCopy: (t) => this.copy(t),
        });
    }

    async copy(text) {
        // navigator.clipboard is async and can reject (permissions / insecure
        // context). Fall back to a legacy execCommand copy, and never throw to
        // the user - worst case we just notify that copy is unavailable.
        try {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                await navigator.clipboard.writeText(text);
            } else {
                this._legacyCopy(text);
            }
            this.notification.add(_t("Copied"), { type: "success" });
        } catch (e) {
            try {
                this._legacyCopy(text);
                this.notification.add(_t("Copied"), { type: "success" });
            } catch (e2) {
                this.notification.add(
                    _t("Copy unavailable; select the text manually."),
                    { type: "warning" },
                );
            }
        }
        this.popover.close();
    }

    _legacyCopy(text) {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "absolute";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
    }
}
