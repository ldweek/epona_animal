# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Bind invoice mirrors back to their verified mirror purchase-order lines."""

from odoo import _, api, models
from odoo.exceptions import UserError

from .intercompany_order_mixin import (
    IC_MIRROR_CAPABILITY,
    IC_MIRROR_CONTEXT,
)


class AccountMove(models.Model):
    _inherit = 'account.move'

    @api.model
    def _eh_apply_mirror_user(self, recordset, config):
        """Keep a configured service identity strictly in target company.

        Base binds the user but historically retained the caller's allowed-
        company context.  A source transaction therefore either failed with
        an unauthorized-company error or exposed more companies than the
        service identity needed.  Order-linked invoice mirrors use the same
        narrow target-only corridor as SO/PO creation.
        """
        result = super()._eh_apply_mirror_user(recordset, config)
        user = config.sudo().intercompany_user_id
        if not user:
            return result
        target = config.sudo().company_id
        return recordset.with_user(user).with_context(
            allowed_company_ids=[target.id],
        ).with_company(target)

    @api.private
    def _eh_ic_purchase_line_for_invoice_line(
        self, source_line, dest_company,
    ):
        """Resolve one sale invoice line to one sealed mirror PO line.

        ``sale_line_ids`` can contain several source lines when invoices are
        grouped.  Linking such a line to an arbitrary PO line would corrupt PO
        billed quantities, so only a unique destination is accepted.
        """
        if 'sale_line_ids' not in source_line._fields:
            return self.env['purchase.order.line']
        source_sale_lines = source_line.sudo().sale_line_ids
        sale_lines = source_sale_lines.filtered(
            lambda line: (
                line.order_id.company_id == self.company_id
                and line.order_id.eh_ic_mirror_po_id
                and line.order_id.eh_ic_mirror_po_id.company_id == dest_company
            )
        )
        if not sale_lines:
            return self.env['purchase.order.line']
        if sale_lines != source_sale_lines:
            raise UserError(_(
                "Invoice line %(line)s combines an inter-company order with "
                "another sale source and cannot be linked safely. Split the "
                "source invoice by inter-company order.",
                line=source_line.display_name,
            ))

        resolved = self.env['purchase.order.line'].sudo()
        for sale_line in sale_lines:
            sale = sale_line.order_id.sudo()
            purchase = sale.eh_ic_mirror_po_id.sudo()
            sale._eh_ic_assert_pair_provenance(sale, purchase)
            sale_scope = sale._eh_ic_commercial_lines(sale)
            purchase_scope = purchase._eh_ic_commercial_lines(purchase)
            try:
                position = sale_scope.ids.index(sale_line.id)
            except ValueError as exc:
                raise UserError(_(
                    "Invoice line %(line)s is outside the sealed "
                    "inter-company order scope.",
                    line=source_line.display_name,
                )) from exc
            resolved |= purchase_scope[position]
        if len(resolved) != 1:
            raise UserError(_(
                "Invoice line %(line)s aggregates more than one mirror "
                "purchase-order line and cannot be linked safely. Split the "
                "source invoice by inter-company order.",
                line=source_line.display_name,
            ))
        return resolved

    def _eh_build_mirror_line_vals(
        self, source_line, dest_company, target_type, config,
    ):
        vals = super()._eh_build_mirror_line_vals(
            source_line, dest_company, target_type, config,
        )
        if (
            target_type in ('in_invoice', 'in_refund')
            and 'purchase_line_id' in self.env['account.move.line']._fields
        ):
            purchase_line = self._eh_ic_purchase_line_for_invoice_line(
                source_line, dest_company,
            )
            if purchase_line:
                vals['purchase_line_id'] = purchase_line.id
        return vals


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    def _sale_create_reinvoice_sale_line(self):
        """Authorize only the core expense-to-sale-line corridor.

        The resulting lines are durably marked as derived and excluded from
        the sealed source/mirror commercial scope.  Direct line creation on a
        bound order remains forbidden.
        """
        order_map = self._sale_determine_order()
        orders = self.env['sale.order'].browse(
            sorted({order.id for order in order_map.values() if order})
        )
        orders._eh_ic_assert_accounting_ready()
        engine_self = self.with_context(**{
            IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
        })
        return super(
            AccountMoveLine, engine_self,
        )._sale_create_reinvoice_sale_line()
