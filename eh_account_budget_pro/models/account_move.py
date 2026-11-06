# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
account.move extension: release commitments when a PO becomes a bill.

When a vendor bill is posted and any of its lines reference back to a
purchase order line (the standard purchase_line_id field on
account.move.line), the corresponding commitment on the
purchase.order is released. This shifts the dollar from `committed`
to `actual` on the budget line: the actual_amount compute picks up
the new posted JE, the committed_amount compute drops the released
row.

Partial billing: we release the commitment proportionally, measured
against the CUMULATIVE amount billed on the PO so far. A first bill
covering 60% of the PO releases 60% of the commitment; a second bill
covering the remaining 40% releases the residual so the encumbrance
lands at exactly zero. The release is computed against each
commitment row's original reserved amount (see original_amount on
eh.budget.commitment), never against an already-reduced current
amount, so sequential partial bills can never leave a phantom
residual reserved forever.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError
from odoo.tools.float_utils import float_compare
from odoo.tools.misc import format_amount

from .budget_commitment import (
    EH_BUDGET_COMMITMENT_CAPABILITY,
    _CAPABILITY_CONTEXT_KEY,
)


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _eh_recompute_po_commitments_after_bill_state_change(self):
        """Rebuild encumbrance from the remaining posted bill evidence."""
        bills = self.filtered(
            lambda move: move.move_type in ('in_invoice', 'in_refund')
        )
        for company in bills.company_id:
            bills.filtered(
                lambda move, c=company: move.company_id == c
            )._eh_release_po_commitments()

    def button_draft(self):
        """Re-reserve the unconsumed PO amount after a posted bill is reset."""
        result = super().button_draft()
        self._eh_recompute_po_commitments_after_bill_state_change()
        return result

    def button_cancel(self):
        """Keep commitments aligned when core cancels a bill directly."""
        result = super().button_cancel()
        # On current Odoo, cancelling a posted move routes through
        # button_draft(), so this second pass is deliberately idempotent. It
        # also protects backports/core variants whose cancel path does not.
        self._eh_recompute_po_commitments_after_bill_state_change()
        return result

    def _post(self, soft=True):
        """Release matching commitments after the bill is posted.

        Runs AFTER super()._post so the line linkage is final
        (Odoo's PO matching may write purchase_line_id during the
        post pipeline). On a multi-bill PO, the first post releases
        the proportion covered by that bill; subsequent posts
        release further proportions.
        """
        result = super()._post(soft=soft)
        bills = self.filtered(
            lambda move: move.state == 'posted'
            and move.move_type in ('in_invoice', 'in_refund')
        )
        # action_post may post several bills in one recordset. Group once by
        # company so bills for the same PO line share one cumulative scan and
        # one deterministic recomputation instead of repeating N times.
        for company in bills.company_id:
            bills.filtered(
                lambda move, c=company: move.company_id == c
            )._eh_release_po_commitments()
        return result

    def _eh_release_po_commitments(self):
        if not self:
            return
        companies = self.company_id
        if len(companies) != 1:
            for company in companies:
                self.filtered(
                    lambda move, c=company: move.company_id == c
                )._eh_release_po_commitments()
            return
        company = companies
        if not self.env.su:
            self._eh_check_access('write')
            if company.id not in self.env.companies.ids:
                raise AccessError(_(
                    "The bill company is outside the actor's allowed companies."
                ))
        Commitment = self.env['eh.budget.commitment'].with_context(**{
            _CAPABILITY_CONTEXT_KEY: EH_BUDGET_COMMITMENT_CAPABILITY,
        }).sudo()
        # Collect every PO this bill touches. We release against the
        # PO's CUMULATIVE billed total (all posted vendor bills to
        # date, including this one), not just this bill in isolation.
        # Sequential partial bills therefore add up: the release is a
        # function of "how much of the PO has been billed so far",
        # measured against each commitment row's original reserved
        # amount, so the second partial bill releases the residual the
        # first left behind instead of scaling an already-reduced row.
        po_orders = self.env['purchase.order']
        for move in self:
            for line in move.invoice_line_ids:
                po_line = getattr(line, 'purchase_line_id', False)
                if not po_line:
                    continue
                if po_line.order_id.company_id != move.company_id:
                    raise AccessError(_(
                        "A vendor bill cannot release commitments from "
                        "another company."
                    ))
                po_orders |= po_line.order_id
        if not po_orders:
            return
        if not self.env.su:
            po_orders._eh_check_access('read')
        if po_orders.filtered(lambda po: po.company_id != company):
            raise AccessError(_(
                "A vendor bill cannot release commitments from another company."
            ))
        # Every transition touching a PO commitment takes this same source
        # lock. A second concurrent bill waits, then recomputes against both
        # committed posted moves instead of overwriting the first release.
        po_orders._eh_lock_budget_source('read')
        all_rows = Commitment.search([
            ('source_model', '=', 'purchase.order'),
            ('source_id', 'in', po_orders.ids),
            ('company_id', '=', company.id),
            ('state', 'in', ('reserved', 'released')),
        ])
        all_rows._eh_lock_rows()
        rows_by_po = {}
        for row in all_rows:
            bucket = rows_by_po.get(row.source_id, Commitment)
            rows_by_po[row.source_id] = bucket | row
        for po in po_orders:
            rows = rows_by_po.get(po.id)
            if not rows:
                continue
            po_lines = po.order_line.browse(rows.mapped('source_line_id'))
            po_lines_by_id = {line.id: line for line in po_lines}
            rows_by_line_currency = {}
            for row in rows:
                po_line = po_lines_by_id.get(row.source_line_id)
                if not po_line or po_line.order_id != po:
                    # Broken provenance never authorises a release against a
                    # different PO line. Preserve row for explicit review.
                    continue
                key = (po_line.id, row.currency_id.id)
                bucket = rows_by_line_currency.get(key, Commitment)
                rows_by_line_currency[key] = bucket | row
            for (po_line_id, _currency_id), line_rows in sorted(
                rows_by_line_currency.items(), key=lambda item: item[0],
            ):
                self._eh_recompute_po_line_commitments(
                    po_lines_by_id[po_line_id], line_rows,
                )

    def _eh_recompute_po_line_commitments(self, po_line, rows):
        """Apply cumulative bill progress once across one PO-line reserve.

        One purchase line may own several commitment rows after analytic
        allocation. It may also reserve only part of its subtotal when some
        analytic scopes have no matching budget. Billing progress therefore
        uses the full PO-line subtotal as denominator, then releases that same
        fraction from every tracked original row. Cumulative allocation keeps
        the aggregate exact in the budget currency; the final row absorbs only
        the deterministic currency-rounding residual.
        """
        if not rows:
            return
        po = po_line.order_id
        po.invalidate_recordset(['state'])
        if po.state not in ('purchase', 'done'):
            # Cancellation is terminal for encumbrance. A related draft bill
            # may still be posted later, but its actual must not resurrect an
            # already released promise from a cancelled source document.
            rows.filtered(lambda row: row.state != 'released').action_release()
            return
        currency = rows[0].currency_id
        ordered = rows.sorted('id')
        originals = []
        for row in ordered:
            original = currency.round(max(
                0.0, row.original_amount or row.amount,
            ))
            originals.append((row, original))
        original_total = currency.round(sum(
            original for _row, original in originals
        ))

        consumed, posted_qty = self._eh_posted_billing_for_po_line(
            po_line, currency,
        )
        rounding = po_line.product_uom_id.rounding or 0.01
        fully_billed = po_line.product_qty > 0 and float_compare(
            posted_qty,
            po_line.product_qty,
            precision_rounding=rounding,
        ) >= 0
        if fully_billed:
            billed_fraction = 1.0
        else:
            line_total = max(0.0, po_line.price_subtotal)
            if po.currency_id != currency:
                effective_date = po_line.date_planned or po.date_order
                if hasattr(effective_date, 'date'):
                    effective_date = effective_date.date()
                line_total = po.currency_id._convert(
                    line_total,
                    currency,
                    po.company_id,
                    effective_date,
                )
            line_total = currency.round(line_total)
            if currency.is_zero(line_total):
                billed_fraction = 1.0 if consumed > 0.0 else 0.0
            else:
                billed_fraction = min(
                    1.0, max(0.0, consumed) / line_total,
                )

        release_total = currency.round(original_total * billed_fraction)
        allocated_release = 0.0
        cumulative_original = 0.0
        for index, (row, original) in enumerate(originals):
            cumulative_original = currency.round(
                cumulative_original + original,
            )
            if index == len(originals) - 1:
                cumulative_target = release_total
            else:
                cumulative_target = currency.round(
                    cumulative_original * billed_fraction,
                )
            released = currency.round(
                cumulative_target - allocated_release,
            )
            released = min(original, max(0.0, released))
            allocated_release = currency.round(
                allocated_release + released,
            )
            target_remaining = currency.round(original - released)
            old_reserved = row.amount if row.state == 'reserved' else 0.0
            if currency.is_zero(target_remaining):
                target_remaining = 0.0
                if row.state != 'released' or row.amount:
                    row.write({'amount': 0.0, 'state': 'released'})
            else:
                row.write({
                    'amount': target_remaining,
                    'state': 'reserved',
                })
            delta = target_remaining - old_reserved
            if not currency.is_zero(delta):
                row.message_post(body=_(
                    "Commitment recomputed after %(move)s: reserved "
                    "amount changed by %(delta)s to %(remaining)s.",
                    move=', '.join(self.mapped('display_name')),
                    delta=format_amount(self.env, delta, currency),
                    remaining=format_amount(
                        self.env, target_remaining, currency,
                    ),
                ))

    def _eh_posted_billing_for_po_line(self, po_line, budget_currency):
        """Posted net amount and quantity for one PO line.

        account.move.line.balance is authoritative company-currency GL value:
        bills are positive expense, refunds negative. Budget currency is the
        same company currency today; explicit conversion keeps this safe if
        that model policy changes later.
        """
        po = po_line.order_id
        companies = self.company_id
        if len(companies) != 1 or po.company_id != companies:
            raise AccessError(_(
                "A vendor bill cannot inspect purchase lines from another company."
            ))
        Line = self.env['account.move.line'].sudo()
        move_lines = Line.search([
            ('purchase_line_id', '=', po_line.id),
            ('move_id.state', '=', 'posted'),
            ('move_id.move_type', 'in', ('in_invoice', 'in_refund')),
            ('company_id', '=', po.company_id.id),
        ])
        total = 0.0
        quantity = 0.0
        for ml in move_lines:
            amount = ml.balance
            if ml.company_currency_id != budget_currency:
                amount = ml.company_currency_id._convert(
                    amount,
                    budget_currency,
                    po.company_id,
                    ml.date,
                )
            total += amount
            line_qty = ml.product_uom_id._compute_quantity(
                ml.quantity, po_line.product_uom_id,
            )
            if ml.move_id.move_type == 'in_refund':
                line_qty = -line_qty
            quantity += line_qty
        return total, quantity
