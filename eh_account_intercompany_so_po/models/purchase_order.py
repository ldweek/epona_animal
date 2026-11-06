# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Mirror a confirmed inter-company purchase order to a draft sales
order in the vendor's company."""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools.float_utils import float_is_zero

from .intercompany_order_mixin import (
    IC_MIRROR_CAPABILITY,
    IC_MIRROR_CONTEXT,
)


class PurchaseOrder(models.Model):
    _name = 'purchase.order'
    _inherit = ['purchase.order', 'eh.ic.order.mixin']

    eh_ic_mirror_so_id = fields.Many2one(
        'sale.order', readonly=True, copy=False,
        string="Inter-company mirror SO",
    )
    eh_ic_origin_so_id = fields.Many2one(
        'sale.order', readonly=True, copy=False, index=True,
        string="Inter-company source SO",
        help="Set when this order was generated as the mirror of a "
             "sales order in another company.",
    )
    eh_ic_provenance_verified = fields.Boolean(
        readonly=True, copy=False, index=True,
        help="Set only on mirror pairs created by the hardened engine.",
    )
    eh_ic_integrity_review_reason = fields.Text(readonly=True, copy=False)
    eh_ic_integrity_review_note = fields.Text(readonly=True, copy=False)
    eh_ic_integrity_reviewed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
        string="Integrity reviewed by",
    )
    eh_ic_integrity_reviewed_at = fields.Datetime(
        readonly=True, copy=False, string="Integrity reviewed at",
    )

    # One mirror sales-order-mirror PO per source sale order per company.
    # PostgreSQL treats NULLs as distinct, so ordinary purchase orders
    # (origin id NULL) are unaffected; this only binds the mirror rows and
    # closes the race where two concurrent confirms both pass the search-
    # before-create guard and create duplicate mirror purchase orders.
    _unique_ic_mirror_po = models.Constraint(
        'unique(eh_ic_origin_so_id, company_id)',
        "An inter-company mirror purchase order already exists for this "
        "source sales order in this company.",
    )

    @staticmethod
    def _eh_ic_provenance_fields():
        return {
            'eh_ic_mirror_so_id', 'eh_ic_origin_so_id',
            'eh_ic_provenance_verified', 'eh_ic_integrity_review_reason',
            'eh_ic_integrity_review_note',
            'eh_ic_integrity_reviewed_by_id',
            'eh_ic_integrity_reviewed_at',
        }

    @staticmethod
    def _eh_ic_commercial_fields():
        return {
            'partner_id', 'company_id', 'currency_id', 'fiscal_position_id',
            'payment_term_id', 'incoterm_id', 'order_line', 'date_order',
            'origin', 'partner_ref',
            'state', 'locked', 'dest_address_id', 'note', 'notes',
            'date_planned',
            'picking_type_id',
            'invoice_status',
        }

    @api.private
    def _eh_ic_lock_commercial_graph(self):
        ids = sorted(self.ids)
        if not ids:
            return self
        self.env.cr.execute(
            "SELECT id FROM purchase_order WHERE id = ANY(%s) "
            "ORDER BY id FOR UPDATE",
            (ids,),
        )
        self.env.cr.execute(
            "SELECT id FROM purchase_order_line WHERE order_id = ANY(%s) "
            "ORDER BY id FOR UPDATE",
            (ids,),
        )
        self.invalidate_recordset([
            'eh_ic_mirror_so_id', 'eh_ic_origin_so_id',
            'eh_ic_provenance_verified', 'order_line',
        ])
        self.order_line.invalidate_recordset([
            name for name in (
                set(self.order_line._eh_ic_commercial_fields())
                | {'eh_ic_derived_line_kind', 'eh_ic_source_tax_summary'}
            )
            if name in self.order_line._fields
        ])
        return self

    @api.private
    def _eh_ic_assert_commercial_mutable(self):
        self._eh_check_access('write')
        ids = sorted(self.ids)
        if not ids:
            return
        self.flush_recordset([
            'eh_ic_mirror_so_id', 'eh_ic_origin_so_id',
        ])
        self.env.cr.execute(
            "SELECT id, eh_ic_mirror_so_id, eh_ic_origin_so_id "
            "FROM purchase_order WHERE id = ANY(%s) "
            "ORDER BY id FOR UPDATE",
            (ids,),
        )
        bound = self.browse([
            order_id for order_id, mirror_id, origin_id in self.env.cr.fetchall()
            if mirror_id or origin_id
        ])
        if bound:
            raise UserError(_(
                "Inter-company order %(orders)s is an immutable commercial "
                "mirror. Cancel and replace the source pair instead of "
                "editing its terms.",
                orders=', '.join(bound.mapped('display_name')),
            ))

    @api.model_create_multi
    def create(self, vals_list):
        if any(
            any(vals.get(field_name)
                for field_name in self._eh_ic_provenance_fields())
            for vals in vals_list
        ) and not self._eh_ic_is_mirror_engine():
            raise AccessError(_(
                "Inter-company purchase-order provenance is server-owned."))
        return super().create(vals_list)

    def write(self, vals):
        if (
            self._eh_ic_provenance_fields().intersection(vals)
            and not self._eh_ic_is_mirror_engine()
        ):
            raise AccessError(_(
                "Inter-company purchase-order provenance is server-owned."))
        if self._eh_ic_commercial_fields().intersection(vals) and not (
                self._eh_ic_is_mirror_engine()) and not (
                self._eh_ic_is_presentation_state_write(vals)):
            self._eh_ic_assert_commercial_mutable()
        return super().write(vals)

    def unlink(self):
        # Both ends are legal-document evidence. Deleting a draft mirror
        # would otherwise SET NULL the source pointer and erase provenance.
        self._eh_check_access('unlink')
        self._eh_ic_assert_commercial_mutable()
        return super().unlink()

    def button_confirm(self):
        if self._eh_ic_is_mirror_engine():
            return super().button_confirm()
        orders = self.sorted('id')
        pairs = []
        for order in orders:
            sale, purchase = order._eh_ic_lifecycle_pair()
            if sale:
                order._eh_ic_assert_not_cancelled(sale, purchase)
            pairs.append((order, sale, purchase))
        engine_orders = orders.with_context(**{
            IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
        })
        result = super(PurchaseOrder, engine_orders).button_confirm()
        for order, sale, purchase in pairs:
            order._eh_ic_create_mirror_so()
            if sale:
                order._eh_ic_sync_pair_lock_after_confirmation(
                    sale, purchase
                )
        return result

    def button_approve(self, force=False):
        if self._eh_ic_is_mirror_engine():
            return super().button_approve(force=force)
        orders = self.sorted('id')
        pairs = []
        for order in orders:
            sale, purchase = order._eh_ic_lifecycle_pair()
            if sale and order.state != 'to approve':
                raise UserError(_(
                    "Only an inter-company RFQ already awaiting approval can "
                    "be approved directly. Use Confirm for a draft RFQ."
                ))
            if sale:
                order._eh_ic_assert_not_cancelled(sale, purchase)
            pairs.append((order, sale, purchase))
        result = super(
            PurchaseOrder,
            orders.with_context(**{
                IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
            }),
        ).button_approve(force=force)
        for order, sale, purchase in pairs:
            if sale:
                order._eh_ic_sync_pair_lock_after_confirmation(
                    sale, purchase
                )
        return result

    def button_cancel(self):
        if self._eh_ic_is_mirror_engine():
            return super().button_cancel()
        result = None
        for order in self.sorted('id'):
            sale, purchase = order._eh_ic_transition_pair()
            if sale:
                order._eh_ic_cancel_pair(sale, purchase)
            else:
                result = super(
                    PurchaseOrder,
                    order.with_context(**{
                        IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
                    }),
                ).button_cancel()
        return result

    def button_draft(self):
        if self._eh_ic_is_mirror_engine():
            return super().button_draft()
        result = {}
        for order in self.sorted('id'):
            sale, purchase = order._eh_ic_transition_pair()
            if sale:
                order._eh_ic_reset_pair(sale, purchase)
            else:
                result = super(
                    PurchaseOrder,
                    order.with_context(**{
                        IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
                    }),
                ).button_draft()
        return result

    def button_lock(self):
        if 'locked' not in self._fields:
            return self.button_done()
        if self._eh_ic_is_mirror_engine():
            return super().button_lock()
        result = True
        for order in self.sorted('id'):
            sale, purchase = order._eh_ic_transition_pair()
            if sale:
                order._eh_ic_set_pair_locked(sale, purchase, True)
            else:
                result = super(
                    PurchaseOrder,
                    order.with_context(**{
                        IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
                    }),
                ).button_lock()
        return result

    def button_done(self):
        """Odoo 16--18 lock entry point; Odoo 19 maps to button_lock."""
        if 'locked' in self._fields:
            return self.button_lock()
        if self._eh_ic_is_mirror_engine():
            return super().button_done()
        result = True
        for order in self.sorted('id'):
            sale, purchase = order._eh_ic_transition_pair()
            if sale:
                order._eh_ic_set_pair_locked(sale, purchase, True)
            else:
                result = super(
                    PurchaseOrder,
                    order.with_context(**{
                        IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
                    }),
                ).button_done()
        return result

    def button_unlock(self):
        if self._eh_ic_is_mirror_engine():
            return super().button_unlock()
        result = True
        for order in self.sorted('id'):
            sale, purchase = order._eh_ic_transition_pair()
            if sale:
                order._eh_ic_set_pair_locked(sale, purchase, False)
            else:
                result = super(
                    PurchaseOrder,
                    order.with_context(**{
                        IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
                    }),
                ).button_unlock()
        return result

    @api.private
    def _eh_ic_assert_accounting_ready(self):
        self._eh_check_access('write')
        for order in self.sorted('id'):
            if not (order.eh_ic_mirror_so_id or order.eh_ic_origin_so_id):
                continue
            if (
                not order.eh_ic_provenance_verified
                or order.eh_ic_integrity_review_reason
            ):
                raise UserError(_(
                    "Legacy or quarantined inter-company purchase order "
                    "%(order)s cannot create a bill until controlled review "
                    "is complete.",
                    order=order.display_name,
                ))
            sale, purchase = order._eh_ic_lifecycle_pair()
            order._eh_ic_assert_not_cancelled(sale, purchase)

    def action_create_invoice(self, attachment_ids=False):
        # Current actor owns this PO; counterpart is server-read only during
        # provenance validation. Core bill creation stays in actor context.
        self._eh_ic_assert_accounting_ready()
        for order in self:
            linked_mirror_bills = order.invoice_ids.filtered(
                lambda move: move.eh_intercompany_origin_id
            )
            product_lines = order.order_line.filtered(
                lambda line: line.product_id and not line.display_type
            )
            if linked_mirror_bills and product_lines and all(
                float_is_zero(
                    line.qty_to_invoice,
                    precision_rounding=(
                        getattr(line, 'product_uom_id', False)
                        or getattr(line, 'product_uom', False)
                    ).rounding,
                )
                for line in product_lines
            ):
                raise UserError(_(
                    "This mirror purchase order is already covered by the "
                    "linked inter-company vendor bill. No second bill can be "
                    "created until a genuine additional invoiceable quantity "
                    "exists."
                ))
        engine_self = self.with_context(**{
            IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
        })
        parent = super(PurchaseOrder, engine_self).action_create_invoice
        if attachment_ids is False:
            # Odoo 16--18 expose a no-argument method. Odoo 19 keeps the same
            # default behaviour when called without the optional attachment
            # payload, so normal bill creation stays cross-series portable.
            return parent()
        return parent(attachment_ids=attachment_ids)

    def _create_downpayments(self, line_vals):
        """Authorize core purchase down-payment lines on a sealed pair."""
        self._eh_ic_assert_accounting_ready()
        engine_self = self.with_context(**{
            IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
        })
        return super(PurchaseOrder, engine_self)._create_downpayments(
            line_vals,
        )

    def _eh_ic_create_mirror_so(self):
        self.ensure_one()
        self._eh_check_access('write')
        self.partner_id._eh_check_access('read')
        if self.eh_ic_origin_so_id or self.eh_ic_mirror_so_id:
            return
        dest = self._eh_ic_dest_company()
        if not dest:
            return
        config = self._eh_ic_config(dest)
        if not config:
            return
        source_lines = self._eh_ic_commercial_lines(self)
        if not source_lines:
            return
        # Resolve the target pricelist/currency on the new SO before any
        # line conversion. Its order currency need not be company currency.
        so_model = self._eh_ic_apply_user(self.env['sale.order'], config)
        target_partner = so_model.env['res.partner'].with_company(dest).browse(
            self.company_id.partner_id.id,
        )
        target_partner._eh_check_access('read')
        target_pricelist = target_partner.property_product_pricelist
        sale_vals = {
            'company_id': dest.id,
            'partner_id': target_partner.id,
            'origin': self.name,
            'date_order': self.date_order,
            'eh_ic_origin_po_id': self.id,
            'eh_ic_provenance_verified': True,
            'eh_ic_integrity_review_reason': False,
        }
        if target_pricelist:
            sale_vals['pricelist_id'] = target_pricelist.id
        if self.partner_ref:
            sale_vals['client_order_ref'] = self.partner_ref
        if self.date_planned:
            sale_vals['commitment_date'] = self.date_planned
        if self.dest_address_id:
            shipping = so_model.env['res.partner'].browse(
                self.dest_address_id.id,
            )
            shipping._eh_check_access('read')
            sale_vals['partner_shipping_id'] = shipping.id
        sale = so_model.with_company(dest).create(sale_vals)
        target_currency = sale.currency_id
        rate_date = fields.Date.to_date(self.date_order) or fields.Date.today()
        # The unit-of-measure field on the target (sale.order.line) is
        # product_uom_id on 18/19 and product_uom on 16/17. Resolve the
        # write key from the target model so create() does not break on the
        # earlier series, mirroring the getattr used on the read side.
        sol_fields = sale.env['sale.order.line']._fields
        uom_key = ('product_uom_id' if 'product_uom_id' in sol_fields
                   else 'product_uom')
        tax_key = 'tax_ids' if 'tax_ids' in sol_fields else 'tax_id'
        line_vals = []
        for pol in source_lines:
            product = sale._eh_ic_target_product(pol.product_id, dest)
            # Carry the SOURCE line unit of measure, not the product
            # default. product_uom_id is the field name on 19; the earlier
            # series call it product_uom, so read whichever exists.
            line_uom = (getattr(pol, 'product_uom_id', False)
                        or getattr(pol, 'product_uom', False))
            target_taxes = sale._eh_ic_filter_target_taxes(
                product.sudo().taxes_id, dest,
            )
            if sale.fiscal_position_id:
                target_taxes = sale.fiscal_position_id.map_tax(target_taxes)
            price_unit, source_discount, tax_summary = (
                sale._eh_ic_mirror_line_economics(
                    pol,
                    product,
                    target_taxes,
                    target_currency,
                    dest,
                    rate_date,
                    target_partner,
                    'discount' in sol_fields,
                )
            )
            values = {
                'product_id': product.id,
                'name': pol.name,
                'product_uom_qty': pol.product_qty,
                uom_key: line_uom.id if line_uom else product.uom_id.id,
                # Mirror uses destination tax defaults.  Explicit value also
                # prevents target-only service user reading source-company tax
                # records during sale-line default/onchange computation.
                tax_key: [(6, 0, target_taxes.ids)],
                'price_unit': price_unit,
                'eh_ic_source_tax_summary': tax_summary,
            }
            if 'discount' in sol_fields:
                values['discount'] = source_discount
            if (
                'product_no_variant_attribute_value_ids' in sol_fields
                and 'product_no_variant_attribute_value_ids' in pol._fields
            ):
                values['product_no_variant_attribute_value_ids'] = [(6, 0,
                    sale._eh_ic_map_no_variant_values(
                        pol.product_no_variant_attribute_value_ids,
                        product,
                    ),
                )]
            line_vals.append((0, 0, values))
        sale.write({'order_line': line_vals})
        self.with_context(**{
            IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
        }).write({
            'eh_ic_mirror_so_id': sale.id,
            'eh_ic_provenance_verified': True,
            'eh_ic_integrity_review_reason': False,
        })
        self.message_post(body=_(
            "Inter-company sales order %(so)s drafted in %(company)s.",
            so=sale.name, company=dest.display_name,
        ))


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    eh_ic_derived_line_kind = fields.Selection([
        ('downpayment', "Down payment"),
        ('expense', "Expense re-invoicing"),
        ('receipt', "Receipt adjustment"),
    ], readonly=True, copy=False, index=True,
       string="Inter-company derived line")
    eh_ic_source_tax_summary = fields.Char(
        readonly=True, copy=False, string="Source tax treatment",
    )

    @staticmethod
    def _eh_ic_provenance_fields():
        return {'eh_ic_derived_line_kind', 'eh_ic_source_tax_summary'}

    @staticmethod
    def _eh_ic_commercial_fields():
        return {
            'order_id', 'product_id', 'name', 'product_qty',
            'product_uom_id', 'product_uom', 'price_unit', 'discount',
            'tax_ids', 'taxes_id', 'analytic_distribution',
            'sequence', 'date_planned', 'display_type', 'is_downpayment',
            'product_no_variant_attribute_value_ids', 'parent_id',
            'product_packaging_id', 'product_packaging_qty',
            'technical_price_unit',
            # Manual receipt and stored billing anchors must never be
            # client-supplied after the header is bound.  ORM compute paths
            # update their cache directly and remain unaffected by this
            # explicit-write guard.
            'qty_received', 'qty_received_manual', 'qty_received_method',
            'qty_invoiced', 'qty_to_invoice', 'invoice_lines',
        }

    def action_eh_ic_set_fulfilled_qty(self, quantity):
        """Controlled dual-company replacement for direct anchor writes."""
        self.ensure_one()
        return self.env['purchase.order']._eh_ic_set_manual_fulfillment(
            self, quantity,
        )

    def action_eh_ic_open_fulfillment_wizard(self):
        self.ensure_one()
        self.env['purchase.order']._eh_ic_assert_integrity_reviewer()
        self._eh_check_access('write')
        return {
            'type': 'ir.actions.act_window',
            'name': _("Record inter-company fulfilment"),
            'res_model': 'eh.ic.manual.fulfillment.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                **self.env.context,
                'default_purchase_line_id': self.id,
                'default_quantity': self.qty_received,
            },
        }

    @api.model_create_multi
    def create(self, vals_list):
        engine = self.env['purchase.order']._eh_ic_is_mirror_engine()
        if (
            any(self._eh_ic_provenance_fields().intersection(vals)
                for vals in vals_list)
            and not engine
        ):
            raise AccessError(_(
                "Inter-company purchase-line provenance is server-owned."
            ))
        vals_list = [dict(vals) for vals in vals_list]
        if engine:
            for vals in vals_list:
                if (
                    not vals.get('eh_ic_derived_line_kind')
                    and vals.get('is_downpayment')
                ):
                    vals['eh_ic_derived_line_kind'] = 'downpayment'
        elif self._eh_ic_verified_receipt_adjustment(vals_list):
            for vals in vals_list:
                vals['eh_ic_derived_line_kind'] = 'receipt'
        else:
            order_ids = {
                vals.get('order_id') for vals in vals_list
                if vals.get('order_id')
            }
            self.env['purchase.order'].browse(order_ids)._eh_ic_assert_commercial_mutable()
        return super().create(vals_list)

    def write(self, vals):
        engine = self.env['purchase.order']._eh_ic_is_mirror_engine()
        if self._eh_ic_provenance_fields().intersection(vals) and not engine:
            raise AccessError(_(
                "Inter-company purchase-line provenance is server-owned."
            ))
        if self._eh_ic_commercial_fields().intersection(vals) and not (
                engine):
            orders = self.order_id
            if vals.get('order_id'):
                orders |= self.env['purchase.order'].browse(vals['order_id'])
            orders._eh_ic_assert_commercial_mutable()
        return super().write(vals)

    @api.model
    @api.private
    def _eh_ic_verified_receipt_adjustment(self, vals_list):
        """Recognize Odoo's stock-receipt synchronization line exactly.

        ``purchase_stock`` creates a zero-ordered-quantity PO line for an
        actually picked, previously unlinked stock move.  The context hint is
        not trusted alone: every linked move must point back to this purchase
        order and match the supplied product.
        """
        if (
            not self.env.context.get('bypass_move_update')
            or 'move_ids' not in self._fields
            or 'stock.move' not in self.env
        ):
            return False
        for vals in vals_list:
            if not vals.get('order_id') or vals.get('product_qty') != 0:
                return False
            move_ids = []
            for command in vals.get('move_ids', []):
                if command and command[0] == 4 and command[1]:
                    move_ids.append(command[1])
            moves = self.env['stock.move'].sudo().browse(move_ids).exists()
            if not moves or len(moves) != len(set(move_ids)):
                return False
            order = self.env['purchase.order'].browse(vals['order_id'])
            if not (order.eh_ic_mirror_so_id or order.eh_ic_origin_so_id):
                return False
            for move in moves:
                purchase = (
                    move.picking_id.purchase_id
                    or move.picking_id.return_id.purchase_id
                )
                if (
                    purchase.id != order.id
                    or move.purchase_line_id
                    or move.product_id.id != vals.get('product_id')
                    or not move.picked
                ):
                    return False
        return True

    def unlink(self):
        if not self.env['purchase.order']._eh_ic_is_mirror_engine():
            self.order_id._eh_ic_assert_commercial_mutable()
        return super().unlink()
