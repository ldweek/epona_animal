# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Mirror a confirmed inter-company sales order to a draft purchase
order in the customer's company."""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .intercompany_order_mixin import (
    IC_MIRROR_CAPABILITY,
    IC_MIRROR_CONTEXT,
)


class SaleOrder(models.Model):
    _name = 'sale.order'
    _inherit = ['sale.order', 'eh.ic.order.mixin']

    eh_ic_mirror_po_id = fields.Many2one(
        'purchase.order', readonly=True, copy=False,
        string="Inter-company mirror PO",
    )
    eh_ic_origin_po_id = fields.Many2one(
        'purchase.order', readonly=True, copy=False, index=True,
        string="Inter-company source PO",
        help="Set when this order was generated as the mirror of a "
             "purchase order in another company.",
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

    # One purchase-order-mirror SO per source purchase order per company.
    # PostgreSQL treats NULLs as distinct, so ordinary sales orders
    # (origin id NULL) are unaffected; this only binds the mirror rows and
    # closes the race where two concurrent confirms both pass the search-
    # before-create guard and create duplicate mirror sales orders.
    _unique_ic_mirror_so = models.Constraint(
        'unique(eh_ic_origin_po_id, company_id)',
        "An inter-company mirror sales order already exists for this "
        "source purchase order in this company.",
    )

    @staticmethod
    def _eh_ic_provenance_fields():
        return {
            'eh_ic_mirror_po_id', 'eh_ic_origin_po_id',
            'eh_ic_provenance_verified', 'eh_ic_integrity_review_reason',
            'eh_ic_integrity_review_note',
            'eh_ic_integrity_reviewed_by_id',
            'eh_ic_integrity_reviewed_at',
        }

    @staticmethod
    def _eh_ic_commercial_fields():
        return {
            'partner_id', 'company_id', 'pricelist_id', 'currency_id',
            'fiscal_position_id', 'payment_term_id', 'order_line', 'date_order',
            # Optional sale_stock fields are still valid write keys when that
            # addon is installed. Strings keep this addon independent from it.
            'incoterm', 'incoterm_location',
            'origin', 'client_order_ref',
            'state', 'locked', 'commitment_date', 'validity_date',
            'journal_id', 'note', 'partner_invoice_id',
            'partner_shipping_id', 'require_signature', 'require_payment',
            'prepayment_percent', 'reference',
            'preferred_payment_method_line_id',
            'invoice_status',
        }

    @api.private
    def _eh_ic_lock_commercial_graph(self):
        """Serialize confirmation and every later commercial mutation."""
        ids = sorted(self.ids)
        if not ids:
            return self
        self.env.cr.execute(
            "SELECT id FROM sale_order WHERE id = ANY(%s) "
            "ORDER BY id FOR UPDATE",
            (ids,),
        )
        self.env.cr.execute(
            "SELECT id FROM sale_order_line WHERE order_id = ANY(%s) "
            "ORDER BY id FOR UPDATE",
            (ids,),
        )
        self.invalidate_recordset([
            'eh_ic_mirror_po_id', 'eh_ic_origin_po_id',
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
            'eh_ic_mirror_po_id', 'eh_ic_origin_po_id',
        ])
        # One header lock serializes an ordinary edit with concurrent mirror
        # binding.  Do not lock or invalidate every line on every ordinary
        # order: the server-owned provenance columns returned by this query
        # are sufficient to decide whether the commercial seal applies.
        self.env.cr.execute(
            "SELECT id, eh_ic_mirror_po_id, eh_ic_origin_po_id "
            "FROM sale_order WHERE id = ANY(%s) "
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
                "Inter-company sales-order provenance is server-owned."))
        return super().create(vals_list)

    def write(self, vals):
        if (
            self._eh_ic_provenance_fields().intersection(vals)
            and not self._eh_ic_is_mirror_engine()
        ):
            raise AccessError(_(
                "Inter-company sales-order provenance is server-owned."))
        if self._eh_ic_commercial_fields().intersection(vals) and not (
                self._eh_ic_is_mirror_engine()) and not (
                self._eh_ic_is_presentation_state_write(vals)):
            self._eh_ic_assert_commercial_mutable()
        return super().write(vals)

    def unlink(self):
        # Preserve both source and mirror legal-document provenance.
        self._eh_check_access('unlink')
        self._eh_ic_assert_commercial_mutable()
        return super().unlink()

    def action_confirm(self):
        if self._eh_ic_is_mirror_engine():
            return super().action_confirm()
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
        result = super(SaleOrder, engine_orders).action_confirm()
        for order, sale, purchase in pairs:
            order._eh_ic_create_mirror_po()
            if sale:
                order._eh_ic_sync_pair_lock_after_confirmation(
                    sale, purchase
                )
        return result

    def action_cancel(self):
        if self._eh_ic_is_mirror_engine():
            return super().action_cancel()
        result = True
        for order in self.sorted('id'):
            sale, purchase = order._eh_ic_transition_pair()
            if sale:
                order._eh_ic_cancel_pair(sale, purchase)
            else:
                result = super(
                    SaleOrder,
                    order.with_context(**{
                        IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
                    }),
                ).action_cancel()
        return result

    def action_draft(self):
        if self._eh_ic_is_mirror_engine():
            return super().action_draft()
        result = True
        for order in self.sorted('id'):
            sale, purchase = order._eh_ic_transition_pair()
            if sale:
                order._eh_ic_reset_pair(sale, purchase)
            else:
                result = super(
                    SaleOrder,
                    order.with_context(**{
                        IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
                    }),
                ).action_draft()
        return result

    def action_lock(self):
        if 'locked' not in self._fields:
            return self.action_done()
        if self._eh_ic_is_mirror_engine():
            return super().action_lock()
        result = True
        for order in self.sorted('id'):
            sale, purchase = order._eh_ic_transition_pair()
            if sale:
                order._eh_ic_set_pair_locked(sale, purchase, True)
            else:
                result = super(
                    SaleOrder,
                    order.with_context(**{
                        IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
                    }),
                ).action_lock()
        return result

    def action_done(self):
        """Odoo 16 lock entry point; Odoo 17+ maps to action_lock."""
        if 'locked' in self._fields:
            return self.action_lock()
        if self._eh_ic_is_mirror_engine():
            return super().action_done()
        result = True
        for order in self.sorted('id'):
            sale, purchase = order._eh_ic_transition_pair()
            if sale:
                order._eh_ic_set_pair_locked(sale, purchase, True)
            else:
                result = super(
                    SaleOrder,
                    order.with_context(**{
                        IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
                    }),
                ).action_done()
        return result

    def action_unlock(self):
        if self._eh_ic_is_mirror_engine():
            return super().action_unlock()
        result = True
        for order in self.sorted('id'):
            sale, purchase = order._eh_ic_transition_pair()
            if sale:
                order._eh_ic_set_pair_locked(sale, purchase, False)
            else:
                result = super(
                    SaleOrder,
                    order.with_context(**{
                        IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
                    }),
                ).action_unlock()
        return result

    @api.private
    def _eh_ic_assert_accounting_ready(self):
        self._eh_check_access('write')
        for order in self.sorted('id'):
            if not (order.eh_ic_mirror_po_id or order.eh_ic_origin_po_id):
                continue
            if (
                not order.eh_ic_provenance_verified
                or order.eh_ic_integrity_review_reason
            ):
                raise UserError(_(
                    "Legacy or quarantined inter-company sales order "
                    "%(order)s cannot create an invoice until controlled "
                    "review is complete.",
                    order=order.display_name,
                ))
            sale, purchase = order._eh_ic_lifecycle_pair()
            order._eh_ic_assert_not_cancelled(sale, purchase)

    def _create_invoices(self, grouped=False, final=False, date=None):
        # Core creates customer invoices with sudo. Prove current-order actor
        # access and validate durable pair provenance before that elevation.
        self._eh_ic_assert_accounting_ready()
        engine_self = self.with_context(**{
            IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
        })
        return super(SaleOrder, engine_self)._create_invoices(
            grouped=grouped,
            final=final,
            date=date,
        )

    def _eh_ic_create_mirror_po(self):
        self.ensure_one()
        self._eh_check_access('write')
        self.partner_id._eh_check_access('read')
        # Never mirror a mirror, never double-mirror.
        if self.eh_ic_origin_po_id or self.eh_ic_mirror_po_id:
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
        # Create the target header first. Purchase currency is a vendor
        # property and can differ from the destination company's currency.
        # Odoo 16/17 do not precompute it during create(), so bind the target
        # property explicitly. The created order remains authoritative for
        # line valuation after core validation/defaults run.
        po_model = self._eh_ic_apply_user(
            self.env['purchase.order'], config)
        target_partner = po_model.env['res.partner'].with_company(dest).browse(
            self.company_id.partner_id.id,
        )
        target_partner._eh_check_access('read')
        target_currency = (
            target_partner.property_purchase_currency_id or dest.currency_id
        )
        purchase_vals = {
            'company_id': dest.id,
            'partner_id': target_partner.id,
            'currency_id': target_currency.id,
            'origin': self.name,
            'date_order': self.date_order,
            'eh_ic_origin_so_id': self.id,
            'eh_ic_provenance_verified': True,
            'eh_ic_integrity_review_reason': False,
        }
        if self.client_order_ref:
            purchase_vals['partner_ref'] = self.client_order_ref
        shipping = self.partner_shipping_id
        # With purchase_stock installed, core treats ``dest_address_id`` as a
        # dropship destination and clears it on ordinary incoming operations.
        # Preserve an explicit alternate SO destination by selecting the one
        # real customer-location dropship operation for the target company.
        # Shipping to the represented-company partner itself remains a normal
        # warehouse receipt and needs no explicit dropship address.
        explicit_shipping = shipping and shipping != self.partner_id
        if (
            explicit_shipping
            and 'dest_address_id' in po_model._fields
            and (not shipping.company_id or shipping.company_id == dest)
        ):
            po_model.env['res.partner'].browse(
                shipping.id,
            )._eh_check_access('read')
            if 'default_location_dest_id_usage' in po_model._fields:
                picking_types = po_model.env[
                    'stock.picking.type'
                ].sudo().with_company(dest).search([
                    ('company_id', '=', dest.id),
                    ('default_location_src_id.usage', '=', 'supplier'),
                    ('default_location_dest_id.usage', '=', 'customer'),
                ], order='id', limit=2)
                if len(picking_types) != 1:
                    raise UserError(_(
                        "Configure exactly one customer dropship operation "
                        "for %(company)s before mirroring an alternate "
                        "shipping destination.",
                        company=dest.display_name,
                    ))
                purchase_vals['picking_type_id'] = picking_types.id
            purchase_vals['dest_address_id'] = shipping.id
        purchase = po_model.with_company(dest).create(purchase_vals)
        target_currency = purchase.currency_id
        rate_date = fields.Date.to_date(self.date_order) or fields.Date.today()
        # The unit-of-measure field on the target (purchase.order.line)
        # is product_uom_id on 18/19 and product_uom on 16/17. Resolve the
        # write key from the target model so create() does not break on the
        # earlier series, mirroring the getattr used on the read side.
        pol_fields = purchase.env['purchase.order.line']._fields
        uom_key = ('product_uom_id' if 'product_uom_id' in pol_fields
                   else 'product_uom')
        tax_key = 'tax_ids' if 'tax_ids' in pol_fields else 'taxes_id'
        line_vals = []
        for sol in source_lines:
            product = purchase._eh_ic_target_product(sol.product_id, dest)
            # Carry the SOURCE line unit of measure, not the product
            # default. product_uom_id is the field name on 19; the earlier
            # series call it product_uom, so read whichever exists.
            line_uom = (getattr(sol, 'product_uom_id', False)
                        or getattr(sol, 'product_uom', False))
            target_taxes = purchase._eh_ic_filter_target_taxes(
                product.sudo().supplier_taxes_id, dest,
            )
            if purchase.fiscal_position_id:
                target_taxes = purchase.fiscal_position_id.map_tax(
                    target_taxes,
                )
            price_unit, source_discount, tax_summary = (
                purchase._eh_ic_mirror_line_economics(
                    sol,
                    product,
                    target_taxes,
                    target_currency,
                    dest,
                    rate_date,
                    target_partner,
                    'discount' in pol_fields,
                )
            )
            values = {
                'product_id': product.id,
                'name': sol.name,
                'product_qty': sol.product_uom_qty,
                uom_key: line_uom.id if line_uom else product.uom_id.id,
                # Resolve destination defaults server-side before target line
                # onchange.  Otherwise a target-only service user can touch a
                # source-company product tax while core is trying to filter it.
                tax_key: [(6, 0, target_taxes.ids)],
                'price_unit': price_unit,
                'date_planned': self.commitment_date or sol._expected_date(),
                'eh_ic_source_tax_summary': tax_summary,
            }
            if 'discount' in pol_fields:
                values['discount'] = source_discount
            if (
                'product_no_variant_attribute_value_ids' in pol_fields
                and 'product_no_variant_attribute_value_ids' in sol._fields
            ):
                values['product_no_variant_attribute_value_ids'] = [(6, 0,
                    purchase._eh_ic_map_no_variant_values(
                        sol.product_no_variant_attribute_value_ids,
                        product,
                    ),
                )]
            line_vals.append((0, 0, values))
        purchase.write({'order_line': line_vals})
        self.with_context(**{
            IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
        }).write({
            'eh_ic_mirror_po_id': purchase.id,
            'eh_ic_provenance_verified': True,
            'eh_ic_integrity_review_reason': False,
        })
        self.message_post(body=_(
            "Inter-company purchase order %(po)s drafted in %(company)s.",
            po=purchase.name, company=dest.display_name,
        ))


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

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
            'order_id', 'product_id', 'name', 'product_uom_qty',
            'product_uom_id', 'product_uom', 'price_unit', 'discount',
            'tax_id', 'tax_ids', 'analytic_distribution',
            'sequence', 'display_type', 'is_downpayment', 'customer_lead',
            'is_expense',
            'product_no_variant_attribute_value_ids', 'parent_id',
            'product_custom_attribute_value_ids',
            'product_template_attribute_value_ids',
            'product_packaging_id', 'product_packaging_qty',
            'route_id', 'route_ids',
            'linked_line_id', 'linked_line_ids', 'combo_item_id',
            'extra_tax_data', 'technical_price_unit',
            # Stored delivery and invoicing anchors are engine-owned once an
            # order is bound.  Core compute assignments do not call write(),
            # while a raw RPC does, so legitimate stock/invoice recomputation
            # remains intact and direct forgery fails closed.
            'qty_delivered', 'qty_delivered_method',
            'qty_invoiced', 'qty_invoiced_posted', 'qty_to_invoice',
            'invoice_status', 'invoice_lines', 'analytic_line_ids',
            'untaxed_amount_invoiced', 'untaxed_amount_to_invoice',
        }

    def action_eh_ic_set_fulfilled_qty(self, quantity):
        """Controlled dual-company replacement for direct anchor writes."""
        self.ensure_one()
        return self.env['sale.order']._eh_ic_set_manual_fulfillment(
            self, quantity,
        )

    def action_eh_ic_open_fulfillment_wizard(self):
        self.ensure_one()
        self.env['sale.order']._eh_ic_assert_integrity_reviewer()
        self._eh_check_access('write')
        return {
            'type': 'ir.actions.act_window',
            'name': _("Record inter-company fulfilment"),
            'res_model': 'eh.ic.manual.fulfillment.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                **self.env.context,
                'default_sale_line_id': self.id,
                'default_quantity': self.qty_delivered,
            },
        }

    @api.model_create_multi
    def create(self, vals_list):
        engine = self.env['sale.order']._eh_ic_is_mirror_engine()
        if (
            any(self._eh_ic_provenance_fields().intersection(vals)
                for vals in vals_list)
            and not engine
        ):
            raise AccessError(_(
                "Inter-company sale-line provenance is server-owned."
            ))
        if engine:
            vals_list = [dict(vals) for vals in vals_list]
            for vals in vals_list:
                if not vals.get('eh_ic_derived_line_kind'):
                    if vals.get('is_downpayment'):
                        vals['eh_ic_derived_line_kind'] = 'downpayment'
                    elif vals.get('is_expense'):
                        vals['eh_ic_derived_line_kind'] = 'expense'
        else:
            order_ids = {
                vals.get('order_id') for vals in vals_list
                if vals.get('order_id')
            }
            self.env['sale.order'].browse(order_ids)._eh_ic_assert_commercial_mutable()
        return super().create(vals_list)

    def write(self, vals):
        engine = self.env['sale.order']._eh_ic_is_mirror_engine()
        if self._eh_ic_provenance_fields().intersection(vals) and not engine:
            raise AccessError(_(
                "Inter-company sale-line provenance is server-owned."
            ))
        if self._eh_ic_commercial_fields().intersection(vals) and not (
                engine):
            orders = self.order_id
            if vals.get('order_id'):
                orders |= self.env['sale.order'].browse(vals['order_id'])
            orders._eh_ic_assert_commercial_mutable()
        return super().write(vals)

    def unlink(self):
        if not self.env['sale.order']._eh_ic_is_mirror_engine():
            # linked_line_id is ON DELETE CASCADE in core.  A line on an
            # otherwise ordinary order can therefore own a bound mirror
            # child; check both sides before PostgreSQL cascades past this
            # model's child unlink guard.
            orders = self.order_id
            if 'linked_line_ids' in self._fields:
                orders |= self.mapped('linked_line_ids.order_id')
            elif 'option_line_ids' in self._fields:
                orders |= self.mapped('option_line_ids.order_id')
            orders._eh_ic_assert_commercial_mutable()
        return super().unlink()


class ProductAttributeCustomValue(models.Model):
    _inherit = 'product.attribute.custom.value'

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env['sale.order']._eh_ic_is_mirror_engine():
            line_ids = {
                vals.get('sale_order_line_id') for vals in vals_list
                if vals.get('sale_order_line_id')
            }
            self.env['sale.order.line'].browse(
                line_ids
            ).mapped('order_id')._eh_ic_assert_commercial_mutable()
        return super().create(vals_list)

    def write(self, vals):
        if not self.env['sale.order']._eh_ic_is_mirror_engine():
            lines = self.mapped('sale_order_line_id')
            if vals.get('sale_order_line_id'):
                lines |= self.env['sale.order.line'].browse(
                    vals['sale_order_line_id']
                )
            lines.mapped('order_id')._eh_ic_assert_commercial_mutable()
        return super().write(vals)

    def unlink(self):
        if not self.env['sale.order']._eh_ic_is_mirror_engine():
            self.mapped(
                'sale_order_line_id.order_id'
            )._eh_ic_assert_commercial_mutable()
        return super().unlink()
