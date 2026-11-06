# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Inter-company SO/PO mirroring tests.

Two companies; a partner that represents company B. Confirming a sales
order in company A drafts a purchase order in B, and confirming a
purchase order in A drafts a sales order in B. A mirror is never
mirrored back, and non-inter-company partners never trigger a mirror.
"""

import ast
import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from psycopg2 import IntegrityError, sql

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.eh_account_intercompany_so_po.models.intercompany_order_mixin import (
    IC_MIRROR_CAPABILITY,
    IC_MIRROR_CONTEXT,
    IC_PAIR_TRANSITION_CAPABILITY,
    IC_PAIR_TRANSITION_CONTEXT,
    IC_PURCHASE_SOURCE_LOCK,
    IC_SALE_SOURCE_LOCK,
)


@tagged('eh_account_intercompany_so_po', 'integration', 'post_install',
        '-at_install')
class TestSoPoMirror(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env.company
        cls.company_b = cls.env['res.company'].create({
            'name': 'SoPo Sister B',
            'currency_id': cls.company_a.currency_id.id,
        })
        cls.env.user.company_ids = [(4, cls.company_b.id)]
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')

        cls.partner_b = cls.env['res.partner'].create({
            'name': 'SoPo Sister B partner',
            'company_id': False,
            'eh_represented_company_id': cls.company_b.id,
        })
        cls.normal_partner = cls.env['res.partner'].create({
            'name': 'Ordinary customer',
        })
        cls.product = cls.env['product.product'].create({
            'name': 'IC product',
            'list_price': 100.0, 'standard_price': 80.0,
        })
        cls.route_manager = new_test_user(
            cls.env,
            login='ic_so_po_route_manager',
            groups=(
                'eh_account_base.group_eh_manager,'
                'sales_team.group_sale_manager,'
                'purchase.group_purchase_manager'
            ),
        )
        cls.contact_user = new_test_user(
            cls.env,
            login='ic_so_po_contact_user',
            groups='base.group_user',
        )
        cls.source_operator = new_test_user(
            cls.env,
            login='ic_so_po_source_operator',
            groups=(
                'sales_team.group_sale_salesman,'
                'purchase.group_purchase_user'
            ),
        )
        cls.lifecycle_user = new_test_user(
            cls.env,
            login='ic_so_po_lifecycle_user',
            groups=(
                'eh_account_base.group_eh_manager,'
                'sales_team.group_sale_manager,'
                'purchase.group_purchase_manager'
            ),
        )
        cls.lifecycle_user.company_ids = [
            (6, 0, (cls.company_a | cls.company_b).ids),
        ]
        cls.target_service_user = new_test_user(
            cls.env,
            login='ic_so_po_target_service',
            groups=(
                'sales_team.group_sale_manager,'
                'purchase.group_purchase_manager,'
                'account.group_account_manager'
            ),
        )
        cls.target_service_user.company_ids = [(4, cls.company_b.id)]
        cls.target_service_user.company_id = cls.company_b
        cls.target_service_user.company_ids = [(6, 0, cls.company_b.ids)]
        cls.config_b = cls.env['eh.intercompany.config'].sudo().create({
            'company_id': cls.company_b.id,
            'enabled': True,
            'intercompany_user_id': cls.target_service_user.id,
        })

    def _as_pair_actor(self, record):
        return record.with_user(self.lifecycle_user).with_context(
            allowed_company_ids=(self.company_a | self.company_b).ids,
        )

    def _as_source_actor(self, record):
        return record.with_user(self.source_operator).with_context(
            allowed_company_ids=self.company_a.ids,
        )

    def _as_target_service(self, record):
        return record.with_user(self.target_service_user).with_context(
            allowed_company_ids=self.company_b.ids,
        ).with_company(self.company_b)

    def _is_locked(self, order):
        return order.locked if 'locked' in order._fields else order.state == 'done'

    def _lock(self, order):
        method = (
            'action_lock' if order._name == 'sale.order' else 'button_lock'
        )
        if 'locked' not in order._fields:
            method = (
                'action_done' if order._name == 'sale.order' else 'button_done'
            )
        return getattr(self._as_pair_actor(order), method)()

    def _invalidate_lifecycle(self, order):
        order.invalidate_recordset([
            field_name for field_name in ('state', 'locked')
            if field_name in order._fields
        ])

    def _sale_order(self, partner):
        return self.env['sale.order'].with_company(self.company_a).create({
            'partner_id': partner.id,
            'company_id': self.company_a.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'name': 'IC sale line',
                'product_uom_qty': 3.0,
                'price_unit': 120.0,
            })],
        })

    def _purchase_order(self, partner):
        return self.env['purchase.order'].with_company(self.company_a).create({
            'partner_id': partner.id,
            'company_id': self.company_a.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'name': 'IC purchase line',
                'product_qty': 5.0,
                'price_unit': 90.0,
                'product_uom_id': self.product.uom_id.id,
            })],
        })

    def _source_sale_order(self):
        sale = self._sale_order(self.partner_b)
        sale.user_id = self.source_operator
        return sale

    def _source_purchase_order(self):
        purchase = self._purchase_order(self.partner_b)
        purchase.user_id = self.source_operator
        return purchase

    # ---- SO -> PO ----

    def test_so_confirm_creates_mirror_po(self):
        sale = self._sale_order(self.partner_b)
        sale.action_confirm()
        po = sale.eh_ic_mirror_po_id
        self.assertTrue(po)
        self.assertEqual(po.company_id, self.company_b)
        self.assertEqual(po.partner_id, self.company_a.partner_id)
        self.assertEqual(po.eh_ic_origin_so_id, sale)
        self.assertTrue(po.eh_ic_provenance_verified)
        self.assertTrue(sale.eh_ic_provenance_verified)
        self.assertEqual(po.state, 'draft')
        self.assertEqual(len(po.order_line), 1)
        self.assertEqual(po.order_line.product_qty, 3.0)
        self.assertEqual(po.order_line.price_unit, 120.0)

    def test_mirror_po_carries_source_uom_and_converted_price(self):
        """A source line in a non-default UoM and a cross-currency order
        must produce a mirror PO line that keeps the SOURCE line UoM (not
        the product default) and a price converted into the destination
        company currency."""
        # A second company/currency for the mirror so a raw price copy
        # would be visibly wrong.
        gbp = self.env.ref('base.GBP')
        eur = self.env.ref('base.EUR')
        cross_company = self.env['res.company'].create({
            'name': 'SoPo Cross-currency B',
            'currency_id': gbp.id,
        })
        self.env.user.company_ids = [(4, cross_company.id)]
        self.company_a.currency_id = eur
        # A deterministic EUR->GBP rate on the order date.
        self.env['res.currency.rate'].create({
            'name': '2026-01-01',
            'currency_id': gbp.id,
            'company_id': cross_company.id,
            'rate': 2.0,
        })
        partner = self.env['res.partner'].create({
            'name': 'SoPo Cross partner',
            'company_id': False,
            'eh_represented_company_id': cross_company.id,
        })
        self.env['eh.intercompany.config'].sudo().create({
            'company_id': cross_company.id,
            'enabled': True,
        })
        # A UoM that is NOT the product default (dozen vs unit).
        dozen = self.env.ref('uom.product_uom_dozen')
        self.assertNotEqual(dozen, self.product.uom_id)
        sale = self.env['sale.order'].with_company(self.company_a).create({
            'partner_id': partner.id,
            'company_id': self.company_a.id,
            'date_order': '2026-06-01',
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'name': 'IC cross line',
                'product_uom_qty': 2.0,
                'product_uom_id': dozen.id,
                'price_unit': 50.0,
            })],
        })
        source_uom = (getattr(sale.order_line, 'product_uom_id', False)
                      or getattr(sale.order_line, 'product_uom', False))
        self.assertEqual(source_uom, dozen)
        sale.action_confirm()
        po = sale.eh_ic_mirror_po_id
        self.assertTrue(po)
        self.assertEqual(po.company_id, cross_company)
        po_uom = (getattr(po.order_line, 'product_uom_id', False)
                  or getattr(po.order_line, 'product_uom', False))
        # Mirror keeps the SOURCE line UoM, not the product default.
        self.assertEqual(po_uom, dozen)
        # 50.0 EUR at rate 2.0 -> 100.0 GBP, not the raw 50.0.
        self.assertAlmostEqual(po.order_line.price_unit, 100.0, places=2)

    def test_no_mirror_for_ordinary_partner(self):
        sale = self._sale_order(self.normal_partner)
        sale.action_confirm()
        self.assertFalse(sale.eh_ic_mirror_po_id)

    def test_mirror_po_uom_write_key_is_version_correct(self):
        """The mirror WRITE side must set the UoM under the field name the
        TARGET line model actually exposes: product_uom_id on 18/19,
        product_uom on 16/17. If the write key were hardcoded to
        product_uom_id the create() would break (or silently drop the UoM)
        on 16/17. Confirming a sale must therefore produce a PO whose line
        carries the source UoM read through the version-correct field."""
        # A UoM that is NOT the product default so a dropped/ignored write
        # would leave the mirror on the product default and fail the check.
        dozen = self.env.ref('uom.product_uom_dozen')
        self.assertNotEqual(dozen, self.product.uom_id)
        pol_key = ('product_uom_id'
                   if 'product_uom_id'
                   in self.env['purchase.order.line']._fields
                   else 'product_uom')
        so_line_key = ('product_uom_id'
                       if 'product_uom_id'
                       in self.env['sale.order.line']._fields
                       else 'product_uom')
        sale = self.env['sale.order'].with_company(self.company_a).create({
            'partner_id': self.partner_b.id,
            'company_id': self.company_a.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'name': 'IC uom line',
                'product_uom_qty': 4.0,
                'price_unit': 30.0,
                so_line_key: dozen.id,
            })],
        })
        sale.action_confirm()
        po = sale.eh_ic_mirror_po_id
        self.assertTrue(po)
        # Read the mirror UoM through the target field name and confirm it
        # equals the source UoM: proves the write key resolved correctly.
        mirror_uom = getattr(po.order_line, pol_key)
        self.assertEqual(mirror_uom, dozen)

    def test_mirror_so_uom_write_key_is_version_correct(self):
        """Symmetric to the SO->PO case: confirming a purchase must set the
        mirror sale line UoM under the field the sale.order.line model
        actually exposes (product_uom_id on 18/19, product_uom on 16/17),
        not a hardcoded product_uom_id that would break on 16/17."""
        dozen = self.env.ref('uom.product_uom_dozen')
        self.assertNotEqual(dozen, self.product.uom_id)
        pol_key = ('product_uom_id'
                   if 'product_uom_id'
                   in self.env['purchase.order.line']._fields
                   else 'product_uom')
        purchase = self.env['purchase.order'].with_company(
            self.company_a).create({
                'partner_id': self.partner_b.id,
                'company_id': self.company_a.id,
                'order_line': [(0, 0, {
                    'product_id': self.product.id,
                    'name': 'IC uom purchase line',
                    'product_qty': 6.0,
                    'price_unit': 20.0,
                    pol_key: dozen.id,
                })],
            })
        purchase.button_confirm()
        sale = purchase.eh_ic_mirror_so_id
        self.assertTrue(sale)
        so_line_key = ('product_uom_id'
                       if 'product_uom_id'
                       in self.env['sale.order.line']._fields
                       else 'product_uom')
        mirror_uom = getattr(sale.order_line, so_line_key)
        self.assertEqual(mirror_uom, dozen)

    # ---- PO -> SO ----

    def test_po_confirm_creates_mirror_so(self):
        purchase = self._purchase_order(self.partner_b)
        purchase.button_confirm()
        sale = purchase.eh_ic_mirror_so_id
        self.assertTrue(sale)
        self.assertEqual(sale.company_id, self.company_b)
        self.assertEqual(sale.partner_id, self.company_a.partner_id)
        self.assertEqual(sale.eh_ic_origin_po_id, purchase)
        self.assertTrue(sale.eh_ic_provenance_verified)
        self.assertTrue(purchase.eh_ic_provenance_verified)
        self.assertEqual(len(sale.order_line), 1)
        self.assertEqual(sale.order_line.product_uom_qty, 5.0)

    def test_mirrors_preserve_discount_and_actual_target_order_currency(self):
        # This regression isolates currency/discount behaviour. Tax semantics
        # have their own focused mixed-inclusion test below.
        self.product.product_tmpl_id.sudo().write({
            'taxes_id': [(5, 0, 0)],
            'supplier_taxes_id': [(5, 0, 0)],
        })
        eur = self.env.ref('base.EUR')
        gbp = self.env.ref('base.GBP')
        rate_date = '2026-04-01'
        purchase_has_discount = (
            'discount' in self.env['purchase.order.line']._fields
        )

        source_pricelist = self.env['product.pricelist'].create({
            'name': 'IC source EUR',
            'currency_id': eur.id,
        })
        self.company_a.partner_id.with_company(self.company_b).write({
            'property_purchase_currency_id': gbp.id,
        })
        sale = self.env['sale.order'].with_company(self.company_a).create({
            'partner_id': self.partner_b.id,
            'company_id': self.company_a.id,
            'pricelist_id': source_pricelist.id,
            'date_order': rate_date,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 2.0,
                'price_unit': 125.0,
                'discount': 12.5,
            })],
        })
        sale.action_confirm()
        purchase = sale.eh_ic_mirror_po_id
        expected_po_price = eur._convert(
            125.0, gbp, self.company_b, rate_date,
        )
        if not purchase_has_discount:
            # Odoo 16 has no purchase-line discount field. The mirror folds
            # source discount into unit price. Core stores that price at the
            # target order currency precision, preserving net economics.
            expected_po_price = gbp.round(
                expected_po_price * (1.0 - (12.5 / 100.0)),
            )
        self.assertEqual(purchase.currency_id, gbp)
        self.assertAlmostEqual(
            purchase.order_line.price_unit, expected_po_price, places=6,
        )
        if purchase_has_discount:
            self.assertAlmostEqual(
                purchase.order_line.discount, 12.5, places=6,
            )

        target_pricelist = self.env['product.pricelist'].create({
            'name': 'IC target GBP',
            'currency_id': gbp.id,
            'company_id': self.company_b.id,
        })
        self.company_a.partner_id.with_company(self.company_b).write({
            'property_product_pricelist': target_pricelist.id,
        })
        self.partner_b.with_company(self.company_a).write({
            'property_purchase_currency_id': eur.id,
        })
        purchase_line_values = {
            'product_id': self.product.id,
            'product_qty': 3.0,
            'price_unit': 80.0,
        }
        purchase_line_values[
            'product_uom_id'
            if 'product_uom_id' in self.env['purchase.order.line']._fields
            else 'product_uom'
        ] = self.product.uom_id.id
        if purchase_has_discount:
            purchase_line_values['discount'] = 7.0
        purchase_source = self.env['purchase.order'].with_company(
            self.company_a,
        ).create({
            'partner_id': self.partner_b.id,
            'company_id': self.company_a.id,
            # Explicit source-order currency: Odoo 16/17 create() does not
            # run the vendor-currency onchange that 18/19 precompute.
            'currency_id': eur.id,
            'date_order': rate_date,
            'order_line': [(0, 0, purchase_line_values)],
        })
        self.assertEqual(purchase_source.currency_id, eur)
        purchase_source.button_confirm()
        target_sale = purchase_source.eh_ic_mirror_so_id
        expected_so_price = eur._convert(
            80.0, gbp, self.company_b, rate_date,
        )
        self.assertEqual(target_sale.currency_id, gbp)
        self.assertAlmostEqual(
            target_sale.order_line.price_unit, expected_so_price, places=6,
        )
        self.assertAlmostEqual(
            target_sale.order_line.discount,
            7.0 if purchase_has_discount else 0.0,
            places=6,
        )

    def test_source_and_mirror_commercial_terms_are_immutable(self):
        sale = self._sale_order(self.partner_b)
        sale.order_line.discount = 15.0
        sale.action_confirm()
        purchase = sale.eh_ic_mirror_po_id
        purchase_has_discount = 'discount' in purchase.order_line._fields
        if purchase_has_discount:
            self.assertEqual(purchase.order_line.discount, 15.0)
        else:
            self.assertAlmostEqual(
                purchase.order_line.price_unit,
                sale.order_line.price_unit * 0.85,
                places=6,
            )

        with self.assertRaises(UserError):
            sale.order_line.write({'price_unit': 1.0})
        with self.assertRaises(UserError):
            sale.write({'date_order': '2027-01-01'})
        with self.assertRaises(UserError):
            purchase.order_line.write(
                {'discount': 0.0}
                if purchase_has_discount else {'price_unit': 0.0}
            )
        with self.assertRaises(UserError):
            purchase.write({'payment_term_id': False})
        with self.assertRaises(UserError):
            purchase.order_line.write({'analytic_distribution': {}})
        with self.assertRaises(UserError):
            purchase.write({'state': 'purchase'})
        with self.assertRaises(UserError):
            purchase.order_line.write({'date_planned': '2027-01-01'})
        with self.assertRaises(UserError):
            purchase.order_line.write({'technical_price_unit': 1.0})
        with self.assertRaises(UserError):
            purchase.unlink()
        with self.assertRaises(UserError):
            sale.write({'payment_term_id': False})
        if 'incoterm' in sale._fields:
            with self.assertRaises(UserError):
                sale.write({'incoterm': False})
        with self.assertRaises(UserError):
            sale.order_line.write({'analytic_distribution': {}})
        with self.assertRaises(UserError):
            sale.write({'state': 'draft'})
        with self.assertRaises(UserError):
            sale.order_line.write({'customer_lead': 99.0})
        with self.assertRaises(UserError):
            sale.order_line.write({'extra_tax_data': {'forged': True}})
        with self.assertRaises(UserError):
            sale.order_line.write({'linked_line_id': False})
        with self.assertRaises(UserError):
            sale.write({'reference': 'FORGED-INVOICE-REFERENCE'})
        with self.assertRaises(UserError):
            sale.unlink()
        with self.assertRaises(UserError):
            self.env['purchase.order.line'].create({
                'order_id': purchase.id,
                'product_id': self.product.id,
                'product_qty': 1.0,
                'product_uom_id': self.product.uom_id.id,
                'price_unit': 1.0,
            })

    def _assert_bound_quantity_anchors_reject_rpc(self, sale, purchase):
        sale_line = self._as_pair_actor(sale.order_line)
        purchase_line = self._as_pair_actor(purchase.order_line)
        sale_values = {
            'qty_delivered': 999.0,
            'qty_delivered_method': 'manual',
            'qty_invoiced': 999.0,
            'qty_invoiced_posted': 999.0,
            'qty_to_invoice': 999.0,
            'invoice_status': 'to invoice',
            'invoice_lines': [(5, 0, 0)],
            'analytic_line_ids': [(5, 0, 0)],
            'untaxed_amount_invoiced': 999.0,
            'untaxed_amount_to_invoice': 999.0,
        }
        purchase_values = {
            'qty_received': 999.0,
            'qty_received_manual': 999.0,
            'qty_received_method': 'manual',
            'qty_invoiced': 999.0,
            'qty_to_invoice': 999.0,
            'invoice_lines': [(5, 0, 0)],
        }
        for field_name, value in sale_values.items():
            if field_name not in sale_line._fields:
                continue
            with self.assertRaisesRegex(UserError, 'immutable commercial'):
                sale_line.with_context(
                    eh_ic_order_mirror_capability=True,
                ).write({field_name: value})
        for field_name, value in purchase_values.items():
            if field_name not in purchase_line._fields:
                continue
            with self.assertRaisesRegex(UserError, 'immutable commercial'):
                purchase_line.with_context(
                    eh_ic_order_mirror_capability=True,
                ).write({field_name: value})

    def test_so_source_and_po_mirror_quantity_anchors_reject_raw_rpc(self):
        sale = self._sale_order(self.partner_b)
        sale.action_confirm()
        self._assert_bound_quantity_anchors_reject_rpc(
            sale, sale.eh_ic_mirror_po_id,
        )

    def test_po_source_and_so_mirror_quantity_anchors_reject_raw_rpc(self):
        purchase = self._purchase_order(self.partner_b)
        purchase.button_confirm()
        self._assert_bound_quantity_anchors_reject_rpc(
            purchase.eh_ic_mirror_so_id, purchase,
        )

    def test_controlled_manual_fulfillment_drives_invoice_and_bill(self):
        manual_product = self.env['product.product'].create({
            'name': 'IC manual fulfilment service',
            # Service quantities stay manual even when full-suite tests install
            # sale_stock/purchase_stock; consumables become stock-move driven.
            'type': 'service',
            'invoice_policy': 'delivery',
            'purchase_method': 'receive',
            'list_price': 150.0,
            'standard_price': 90.0,
        })
        sale = self.env['sale.order'].with_company(self.company_a).create({
            'partner_id': self.partner_b.id,
            'company_id': self.company_a.id,
            'order_line': [(0, 0, {
                'product_id': manual_product.id,
                'product_uom_qty': 3.0,
                'price_unit': 150.0,
            })],
        })
        sale.action_confirm()
        purchase = sale.eh_ic_mirror_po_id
        purchase.with_company(self.company_b).button_confirm()
        self.assertEqual(sale.order_line.qty_delivered_method, 'manual')
        self.assertEqual(purchase.order_line.qty_received_method, 'manual')

        with self.assertRaises(AccessError):
            sale.order_line.with_user(
                self.contact_user
            ).action_eh_ic_set_fulfilled_qty(2.0)
        self._as_pair_actor(
            sale.order_line
        ).action_eh_ic_set_fulfilled_qty(2.0)
        sale.order_line.invalidate_recordset()
        purchase.order_line.invalidate_recordset()
        self.assertEqual(sale.order_line.qty_delivered, 2.0)
        self.assertEqual(purchase.order_line.qty_received, 2.0)
        self.assertEqual(purchase.order_line.qty_received_manual, 2.0)
        self.assertEqual(sale.order_line.qty_to_invoice, 2.0)
        self.assertEqual(purchase.order_line.qty_to_invoice, 2.0)

        with self.assertRaisesRegex(UserError, 'monotonic'):
            self._as_pair_actor(
                purchase.order_line
            ).action_eh_ic_set_fulfilled_qty(1.0)
        with self.assertRaisesRegex(UserError, 'sealed ordered quantity'):
            self._as_pair_actor(
                sale.order_line
            ).action_eh_ic_set_fulfilled_qty(4.0)

        sale_journal_a = self.env['account.journal'].sudo().create({
            'name': 'IC Manual Sale A',
            'code': 'IMSA',
            'type': 'sale',
            'company_id': self.company_a.id,
        })
        purchase_journal_b = self.env['account.journal'].sudo().create({
            'name': 'IC Manual Purchase B',
            'code': 'IMPB',
            'type': 'purchase',
            'company_id': self.company_b.id,
        })
        income_a = self.env['account.account'].sudo().with_company(
            self.company_a
        ).create({
            'code': '419904',
            'name': 'IC Manual Income A',
            'account_type': 'income',
            'company_ids': [(6, 0, self.company_a.ids)],
        })
        receivable_a = self.env['account.account'].sudo().with_company(
            self.company_a
        ).create({
            'code': '119904',
            'name': 'IC Manual Receivable A',
            'account_type': 'asset_receivable',
            'company_ids': [(6, 0, self.company_a.ids)],
            'reconcile': True,
        })
        expense_b = self.env['account.account'].sudo().with_company(
            self.company_b
        ).create({
            'code': '619904',
            'name': 'IC Manual Expense B',
            'account_type': 'expense',
            'company_ids': [(6, 0, self.company_b.ids)],
        })
        payable_b = self.env['account.account'].sudo().with_company(
            self.company_b
        ).create({
            'code': '219904',
            'name': 'IC Manual Payable B',
            'account_type': 'liability_payable',
            'company_ids': [(6, 0, self.company_b.ids)],
            'reconcile': True,
        })
        manual_product.product_tmpl_id.with_company(self.company_a).write({
            'property_account_income_id': income_a.id,
        })
        manual_product.product_tmpl_id.with_company(self.company_b).write({
            'property_account_expense_id': expense_b.id,
        })
        self.partner_b.with_company(self.company_a).write({
            'property_account_receivable_id': receivable_a.id,
        })
        self.company_a.partner_id.with_company(self.company_b).write({
            'property_account_payable_id': payable_b.id,
        })
        invoice = sale.with_context(
            default_journal_id=sale_journal_a.id,
        )._create_invoices()
        self.assertTrue(invoice)
        ordinary_sale = self._sale_order(self.normal_partner)
        invoice.invoice_line_ids.write({
            'sale_line_ids': [(4, ordinary_sale.order_line.id)],
        })
        with self.assertRaisesRegex(UserError, 'combines an inter-company'):
            invoice._eh_ic_purchase_line_for_invoice_line(
                invoice.invoice_line_ids, self.company_b,
            )
        invoice.invoice_line_ids.write({
            'sale_line_ids': [(3, ordinary_sale.order_line.id)],
        })
        self.config_b.sudo().write({
            'purchase_journal_id': purchase_journal_b.id,
            'fallback_expense_account_id': expense_b.id,
        })
        invoice.action_post()
        invoice.invalidate_recordset(['eh_intercompany_mirror_id'])
        mirror_bill = invoice.eh_intercompany_mirror_id
        self.assertTrue(mirror_bill)
        self.assertEqual(mirror_bill.move_type, 'in_invoice')
        self.assertEqual(
            mirror_bill.invoice_line_ids.purchase_line_id,
            purchase.order_line,
        )
        purchase.invalidate_recordset(['invoice_ids'])
        purchase.order_line.invalidate_recordset([
            'invoice_lines', 'qty_invoiced', 'qty_to_invoice',
        ])
        self.assertIn(mirror_bill, purchase.invoice_ids)
        self.assertEqual(purchase.order_line.qty_invoiced, 2.0)
        self.assertEqual(purchase.order_line.qty_to_invoice, 0.0)
        invoice_ids_before = purchase.invoice_ids.ids
        with self.assertRaisesRegex(UserError, 'already covered'):
            purchase.with_context(
                default_journal_id=purchase_journal_b.id,
            ).action_create_invoice()
        purchase.invalidate_recordset(['invoice_ids'])
        self.assertEqual(purchase.invoice_ids.ids, invoice_ids_before)

    def test_bound_draft_orders_allow_core_send_and_print_markers(self):
        sale = self._sale_order(self.partner_b)
        sale.action_confirm()
        purchase = sale.eh_ic_mirror_po_id
        self.assertEqual(purchase.state, 'draft')
        purchase.print_quotation()
        purchase.invalidate_recordset(['state'])
        self.assertEqual(purchase.state, 'sent')

        purchase_source = self._purchase_order(self.partner_b)
        purchase_source.button_confirm()
        mirror_sale = purchase_source.eh_ic_mirror_so_id
        self.assertEqual(mirror_sale.state, 'draft')
        mirror_sale.action_quotation_sent()
        mirror_sale.invalidate_recordset(['state'])
        self.assertEqual(mirror_sale.state, 'sent')

    def test_quarantined_pair_requires_dual_manager_attestation(self):
        sale = self._sale_order(self.partner_b)
        sale.action_confirm()
        purchase = sale.eh_ic_mirror_po_id
        reason = 'Legacy pair awaiting documentary review'
        engine_context = {IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY}
        sale.with_context(**engine_context).write({
            'eh_ic_provenance_verified': False,
            'eh_ic_integrity_review_reason': reason,
        })
        purchase.with_context(**engine_context).write({
            'eh_ic_provenance_verified': False,
            'eh_ic_integrity_review_reason': reason,
        })
        sale.invalidate_recordset()
        purchase.invalidate_recordset()

        with self.assertRaises(AccessError):
            self._as_source_actor(sale)._eh_ic_review_integrity_pair(
                'Reviewed source and mirror documents.',
            )
        self._as_pair_actor(sale)._eh_ic_review_integrity_pair(
            'Reviewed reciprocal links, companies, route, products and quantities.',
        )
        sale.invalidate_recordset()
        purchase.invalidate_recordset()
        self.assertTrue(sale.eh_ic_provenance_verified)
        self.assertTrue(purchase.eh_ic_provenance_verified)
        self.assertFalse(sale.eh_ic_integrity_review_reason)
        self.assertEqual(
            sale.eh_ic_integrity_reviewed_by_id, self.lifecycle_user,
        )
        self.assertTrue(sale.eh_ic_integrity_reviewed_at)
        self.assertIn('reciprocal links', sale.eh_ic_integrity_review_note)

        self._as_pair_actor(sale).action_cancel()
        self._invalidate_lifecycle(sale)
        self._invalidate_lifecycle(purchase)
        self.assertEqual((sale.state, purchase.state), ('cancel', 'cancel'))

    def test_integrity_review_never_repairs_a_corrupt_pair(self):
        sale = self._sale_order(self.partner_b)
        sale.action_confirm()
        purchase = sale.eh_ic_mirror_po_id
        engine_context = {IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY}
        sale.with_context(**engine_context).write({
            'eh_ic_provenance_verified': False,
            'eh_ic_integrity_review_reason': 'quarantined',
        })
        purchase.with_context(**engine_context).write({
            'eh_ic_provenance_verified': False,
            'eh_ic_integrity_review_reason': 'quarantined',
        })
        purchase.order_line.flush_recordset(['product_qty'])
        self.env.cr.execute(
            "UPDATE purchase_order_line SET product_qty = product_qty + 1 "
            "WHERE order_id = %s",
            (purchase.id,),
        )
        sale.invalidate_recordset()
        purchase.invalidate_recordset()
        purchase.order_line.invalidate_recordset()
        with self.assertRaisesRegex(UserError, 'product/quantity'):
            self._as_pair_actor(sale)._eh_ic_review_integrity_pair(
                'Attempted review of damaged pair.',
            )
        sale.invalidate_recordset(['eh_ic_provenance_verified'])
        purchase.invalidate_recordset(['eh_ic_provenance_verified'])
        self.assertFalse(sale.eh_ic_provenance_verified)
        self.assertFalse(purchase.eh_ic_provenance_verified)

    def test_so_to_po_lifecycle_is_atomic_in_both_states(self):
        sale = self._sale_order(self.partner_b)
        sale.action_confirm()
        purchase = sale.eh_ic_mirror_po_id

        self._as_pair_actor(sale).action_cancel()
        self._invalidate_lifecycle(sale)
        self._invalidate_lifecycle(purchase)
        self.assertEqual((sale.state, purchase.state), ('cancel', 'cancel'))
        with self.assertRaisesRegex(UserError, 'cancelled inter-company'):
            self._as_pair_actor(purchase).button_confirm()

        self._as_pair_actor(sale).action_draft()
        self.assertEqual((sale.state, purchase.state), ('draft', 'draft'))
        self._as_pair_actor(sale).action_confirm()
        self.assertEqual((sale.state, purchase.state), ('sale', 'draft'))
        self._as_pair_actor(purchase).button_confirm()
        self.assertEqual((sale.state, purchase.state), ('sale', 'purchase'))

        self._lock(sale)
        self.assertTrue(self._is_locked(sale))
        self.assertTrue(self._is_locked(purchase))
        self._as_pair_actor(purchase).button_unlock()
        self.assertFalse(self._is_locked(sale))
        self.assertFalse(self._is_locked(purchase))

    def test_po_to_so_lifecycle_is_atomic_in_both_states(self):
        purchase = self._purchase_order(self.partner_b)
        purchase.button_confirm()
        sale = purchase.eh_ic_mirror_so_id

        self._as_pair_actor(purchase).button_cancel()
        self._invalidate_lifecycle(sale)
        self._invalidate_lifecycle(purchase)
        self.assertEqual((sale.state, purchase.state), ('cancel', 'cancel'))
        with self.assertRaisesRegex(UserError, 'cancelled inter-company'):
            self._as_pair_actor(sale).action_confirm()

        self._as_pair_actor(purchase).button_draft()
        self.assertEqual((sale.state, purchase.state), ('draft', 'draft'))
        self._as_pair_actor(purchase).button_confirm()
        self.assertEqual((sale.state, purchase.state), ('draft', 'purchase'))
        self._as_pair_actor(sale).action_confirm()
        self.assertEqual((sale.state, purchase.state), ('sale', 'purchase'))

        self._lock(purchase)
        self.assertTrue(self._is_locked(sale))
        self.assertTrue(self._is_locked(purchase))
        self._as_pair_actor(sale).action_unlock()
        self.assertFalse(self._is_locked(sale))
        self.assertFalse(self._is_locked(purchase))

    def test_source_service_pair_transition_never_elevates_target_actor(self):
        sale = self._source_sale_order()
        self.assertNotIn(self.company_b, self.source_operator.company_ids)
        self._as_source_actor(sale).action_confirm()
        sale.invalidate_recordset()
        purchase = sale.eh_ic_mirror_po_id
        self._as_target_service(purchase).button_confirm()

        # Target actor may change its own order, but cannot use configured
        # service identity to cancel canonical source in another company.
        with self.assertRaises(AccessError):
            self._as_target_service(purchase).button_cancel()
        sale.invalidate_recordset(['state'])
        purchase.invalidate_recordset(['state'])
        self.assertEqual((sale.state, purchase.state), ('sale', 'purchase'))

        # Canonical source actor remains source-authorised; configured service
        # handles only target half of atomic pair transition.
        self._as_source_actor(sale).action_cancel()
        sale.invalidate_recordset(['state'])
        purchase.invalidate_recordset(['state'])
        self.assertEqual((sale.state, purchase.state), ('cancel', 'cancel'))

    def test_both_directions_share_source_advisory_lock_across_cursors(self):
        sale_source = self._sale_order(self.partner_b)
        sale_source.action_confirm()
        purchase_mirror = sale_source.eh_ic_mirror_po_id
        expected_sale_key = IC_SALE_SOURCE_LOCK + sale_source.id
        self.assertEqual(
            purchase_mirror._eh_ic_candidate_source_lock_keys(),
            [expected_sale_key],
        )
        sale_source._eh_ic_lifecycle_pair()

        purchase_source = self._purchase_order(self.partner_b)
        purchase_source.button_confirm()
        sale_mirror = purchase_source.eh_ic_mirror_so_id
        expected_purchase_key = IC_PURCHASE_SOURCE_LOCK + purchase_source.id
        self.assertEqual(
            sale_mirror._eh_ic_candidate_source_lock_keys(),
            [expected_purchase_key],
        )
        purchase_source._eh_ic_lifecycle_pair()

        # A genuinely independent backend cannot enter either source-level
        # critical section while this transaction holds it.  No committed ORM
        # fixture is needed because PostgreSQL advisory locks are cluster-wide.
        with self.env.registry.cursor() as other_cr:
            other_cr.execute(
                "SELECT pg_try_advisory_xact_lock(%s)",
                (expected_sale_key,),
            )
            self.assertFalse(other_cr.fetchone()[0])
            other_cr.execute(
                "SELECT pg_try_advisory_xact_lock(%s)",
                (expected_purchase_key,),
            )
            self.assertFalse(other_cr.fetchone()[0])
            other_cr.rollback()

    def test_bound_linked_line_survives_external_parent_delete_attempt(self):
        if 'linked_line_id' not in self.env['sale.order.line']._fields:
            self.skipTest('core linked sale-line cascade is unavailable')
        external = self._sale_order(self.normal_partner)
        source = self._sale_order(self.partner_b)
        source.order_line.write({'linked_line_id': external.order_line.id})
        source.action_confirm()
        source_line_id = source.order_line.id
        with self.assertRaises(UserError):
            external.order_line.unlink()
        self.assertTrue(
            self.env['sale.order.line'].browse(source_line_id).exists()
        )

    def test_pre_migration_preserves_and_rejects_legacy_duplicates(self):
        sale = self._sale_order(self.normal_partner)
        orders = self.env['purchase.order'].with_company(
            self.company_b,
        ).create([{
            'company_id': self.company_b.id,
            'partner_id': self.company_a.partner_id.id,
        }, {
            'company_id': self.company_b.id,
            'partner_id': self.company_a.partner_id.id,
        }])
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'purchase_order'::regclass "
            "AND pg_get_constraintdef(oid) LIKE "
            "'UNIQUE (eh_ic_origin_so_id, company_id)%'"
        )
        constraint = self.env.cr.fetchone()
        self.assertTrue(constraint)
        self.env.cr.execute(sql.SQL(
            "ALTER TABLE purchase_order DROP CONSTRAINT {}"
        ).format(sql.Identifier(constraint[0])))
        self.env.cr.execute(
            "UPDATE purchase_order SET eh_ic_origin_so_id = %s "
            "WHERE id = ANY(%s)",
            (sale.id, orders.ids),
        )
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.0.2' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_ic_so_po_pre_102', path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        with self.assertRaisesRegex(RuntimeError, 'no order or provenance'):
            migration.migrate(self.env.cr, '19.0.1.0.1')
        self.env.cr.execute(
            "SELECT id, eh_ic_origin_so_id FROM purchase_order "
            "WHERE id = ANY(%s) ORDER BY id",
            (orders.ids,),
        )
        self.assertEqual(
            self.env.cr.fetchall(),
            [(orders[0].id, sale.id), (orders[1].id, sale.id)],
        )

    def test_upgrade_rejects_unproved_singleton_mirror_provenance(self):
        sale = self._sale_order(self.normal_partner)
        purchase = self.env['purchase.order'].with_company(
            self.company_b,
        ).create({
            'company_id': self.company_b.id,
            'partner_id': self.company_a.partner_id.id,
        })
        self.env.flush_all()
        # Pre-hardening clients could forge only one end. Uniqueness sees no
        # duplicate, but this is not a reciprocal engine-created pair.
        self.env.cr.execute(
            "UPDATE purchase_order SET eh_ic_origin_so_id = %s WHERE id = %s",
            (sale.id, purchase.id),
        )
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.0.3' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_ic_so_po_pre_103', path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.0.2')
        self.env.cr.execute(
            "SELECT eh_ic_origin_so_id, eh_ic_provenance_verified, "
            "eh_ic_integrity_review_reason FROM purchase_order WHERE id = %s",
            (purchase.id,),
        )
        origin_id, verified, reason = self.env.cr.fetchone()
        self.assertEqual(origin_id, sale.id)
        self.assertFalse(verified)
        self.assertIn('preserved for controlled review', reason)
        purchase.invalidate_recordset()
        with self.assertRaisesRegex(UserError, 'controlled review'):
            purchase.button_confirm()

    def test_upgrade_never_blesses_self_consistent_legacy_pair(self):
        sale = self._sale_order(self.normal_partner)
        purchase = self.env['purchase.order'].with_company(
            self.company_b,
        ).create({
            'company_id': self.company_b.id,
            'partner_id': self.company_a.partner_id.id,
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE purchase_order SET eh_ic_origin_so_id = %s WHERE id = %s",
            (sale.id, purchase.id),
        )
        self.env.cr.execute(
            "UPDATE sale_order SET eh_ic_mirror_po_id = %s WHERE id = %s",
            (purchase.id, sale.id),
        )
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.0.3' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_ic_so_po_pre_103_pair', path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.0.2')
        self.env.cr.execute(
            "SELECT eh_ic_provenance_verified FROM sale_order WHERE id = %s",
            (sale.id,),
        )
        self.assertEqual(self.env.cr.fetchone(), (False,))
        self.env.cr.execute(
            "SELECT eh_ic_provenance_verified FROM purchase_order "
            "WHERE id = %s",
            (purchase.id,),
        )
        self.assertEqual(self.env.cr.fetchone(), (False,))

    def test_confirmed_legacy_pair_cannot_invoice_or_bill_after_quarantine(self):
        sale = self._sale_order(self.normal_partner)
        purchase = self.env['purchase.order'].with_company(
            self.company_b,
        ).create({
            'company_id': self.company_b.id,
            'partner_id': self.company_a.partner_id.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_qty': 3.0,
                'product_uom_id': self.product.uom_id.id,
                'price_unit': 120.0,
            })],
        })
        self.env.flush_all()
        self.env.cr.execute("""
            UPDATE sale_order
               SET state = 'sale', eh_ic_mirror_po_id = %s
             WHERE id = %s
        """, (purchase.id, sale.id))
        self.env.cr.execute("""
            UPDATE purchase_order
               SET state = 'purchase', eh_ic_origin_so_id = %s
             WHERE id = %s
        """, (sale.id, purchase.id))
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.0.3' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_ic_so_po_pre_103_confirmed', path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.0.2')
        sale.invalidate_recordset()
        purchase.invalidate_recordset()

        with self.assertRaisesRegex(UserError, 'controlled review'):
            sale._create_invoices()
        with self.assertRaisesRegex(UserError, 'controlled review'):
            purchase.action_create_invoice()

    def test_104_migration_quarantines_verified_cancel_mismatch(self):
        sale = self._sale_order(self.partner_b)
        sale.action_confirm()
        purchase = sale.eh_ic_mirror_po_id
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE sale_order SET state = 'cancel' WHERE id = %s",
            (sale.id,),
        )
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.0.4' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_ic_so_po_pre_104_mismatch', path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.0.3')
        sale.invalidate_recordset()
        purchase.invalidate_recordset()
        self.assertFalse(sale.eh_ic_provenance_verified)
        self.assertFalse(purchase.eh_ic_provenance_verified)
        self.assertIn('controlled review', sale.eh_ic_integrity_review_reason)
        self.assertIn(
            'controlled review', purchase.eh_ic_integrity_review_reason,
        )
        with self.assertRaisesRegex(UserError, 'controlled review'):
            purchase.button_confirm()

    def test_104_migration_quarantines_verified_one_sided_fk(self):
        sale = self._sale_order(self.normal_partner)
        purchase = self.env['purchase.order'].with_company(
            self.company_b,
        ).create({
            'company_id': self.company_b.id,
            'partner_id': self.company_a.partner_id.id,
        })
        self.env.flush_all()
        self.env.cr.execute("""
            UPDATE sale_order
               SET eh_ic_mirror_po_id = %s,
                   eh_ic_provenance_verified = TRUE
             WHERE id = %s
        """, (purchase.id, sale.id))
        self.env.cr.execute("""
            UPDATE purchase_order
               SET eh_ic_provenance_verified = TRUE
             WHERE id = %s
        """, (purchase.id,))
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.0.4' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_ic_so_po_pre_104_one_sided', path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.0.3')
        sale.invalidate_recordset()
        purchase.invalidate_recordset()
        self.assertFalse(sale.eh_ic_provenance_verified)
        self.assertFalse(purchase.eh_ic_provenance_verified)
        self.assertIn('controlled review', sale.eh_ic_integrity_review_reason)
        self.assertIn(
            'controlled review', purchase.eh_ic_integrity_review_reason,
        )

    def test_114_migration_backfills_only_explicit_derived_markers(self):
        sale = self._sale_order(self.partner_b)
        sale.action_confirm()
        purchase = sale.eh_ic_mirror_po_id
        engine_context = {IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY}
        downpayment = self.env['sale.order.line'].with_context(
            **engine_context,
        ).create({
            'order_id': sale.id,
            'product_id': self.product.id,
            'product_uom_qty': 0.0,
            'price_unit': 10.0,
            'is_downpayment': True,
        })
        expense = self.env['sale.order.line'].with_context(
            **engine_context,
        ).create({
            'order_id': sale.id,
            'product_id': self.product.id,
            'product_uom_qty': 1.0,
            'price_unit': 20.0,
            'is_expense': True,
        })
        purchase_downpayment = self.env['purchase.order.line']
        if 'is_downpayment' in purchase_downpayment._fields:
            purchase_downpayment = purchase_downpayment.with_context(
                **engine_context,
            ).create({
                'order_id': purchase.id,
                'product_id': self.product.id,
                'product_qty': 0.0,
                'product_uom_id': self.product.uom_id.id,
                'price_unit': 30.0,
                'is_downpayment': True,
            })
        ambiguous_receipt = self.env['purchase.order.line'].with_context(
            **engine_context,
        ).create({
            'order_id': purchase.id,
            'product_id': self.product.id,
            'product_qty': 0.0,
            'product_uom_id': self.product.uom_id.id,
            'price_unit': 0.0,
            'eh_ic_derived_line_kind': 'receipt',
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE sale_order_line SET eh_ic_derived_line_kind = NULL "
            "WHERE id IN %s",
            [tuple((downpayment | expense).ids)],
        )
        purchase_ids = (purchase_downpayment | ambiguous_receipt).ids
        self.env.cr.execute(
            "UPDATE purchase_order_line "
            "SET eh_ic_derived_line_kind = NULL WHERE id IN %s",
            [tuple(purchase_ids)],
        )

        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.1.4' / 'post-migration.py'
        )
        spec = spec_from_file_location('eh_ic_so_po_post_114', path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.1.3')
        (downpayment | expense).invalidate_recordset([
            'eh_ic_derived_line_kind',
        ])
        (purchase_downpayment | ambiguous_receipt).invalidate_recordset([
            'eh_ic_derived_line_kind',
        ])
        self.assertEqual(downpayment.eh_ic_derived_line_kind, 'downpayment')
        self.assertEqual(expense.eh_ic_derived_line_kind, 'expense')
        if purchase_downpayment:
            self.assertEqual(
                purchase_downpayment.eh_ic_derived_line_kind,
                'downpayment',
            )
        self.assertFalse(ambiguous_receipt.eh_ic_derived_line_kind)

    # ---- no mirror-of-a-mirror ----

    def test_mirror_po_not_remirrored_on_confirm(self):
        sale = self._sale_order(self.partner_b)
        sale.action_confirm()
        po = sale.eh_ic_mirror_po_id
        # Confirming the generated PO must not create a back-mirror SO.
        po.with_company(self.company_b).button_confirm()
        self.assertFalse(po.eh_ic_mirror_so_id)

    # ---- concurrency guard ----

    def test_unique_constraint_blocks_duplicate_mirror_po(self):
        """The unique(origin_so, company) constraint forbids a second
        purchase order claiming the same source sales order in the same
        company, closing the concurrent double-mirror race."""
        sale = self._sale_order(self.partner_b)
        sale.action_confirm()
        self.assertTrue(sale.eh_ic_mirror_po_id)
        # A second PO in company B trying to claim the same source SO
        # (already taken by the real mirror) must fail the DB constraint.
        other = self.env['purchase.order'].sudo().with_company(
            self.company_b).create({
                'company_id': self.company_b.id,
                'partner_id': self.company_a.partner_id.id,
            })
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                other.with_context(**{
                    IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
                }).write({'eh_ic_origin_so_id': sale.id})
                other.flush_recordset(['eh_ic_origin_so_id'])

    def test_unique_constraint_blocks_duplicate_mirror_so(self):
        """The unique(origin_po, company) constraint forbids a second
        sales order claiming the same source purchase order in the same
        company, closing the concurrent double-mirror race."""
        purchase = self._purchase_order(self.partner_b)
        purchase.button_confirm()
        self.assertTrue(purchase.eh_ic_mirror_so_id)
        # A second SO in company B trying to claim the same source PO
        # (already taken by the real mirror) must fail the DB constraint.
        other = self.env['sale.order'].sudo().with_company(
            self.company_b).create({
                'company_id': self.company_b.id,
                'partner_id': self.company_a.partner_id.id,
            })
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                other.with_context(**{
                    IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
                }).write({'eh_ic_origin_po_id': purchase.id})
                other.flush_recordset(['eh_ic_origin_po_id'])

    # ---- security ownership ----

    def test_contact_writer_cannot_manufacture_intercompany_route(self):
        with self.assertRaises(AccessError):
            self.normal_partner.with_user(self.contact_user).write({
                'eh_represented_company_id': self.company_b.id,
            })
        with self.assertRaises(AccessError):
            self.normal_partner.with_user(self.route_manager).write({
                'eh_represented_company_id': self.company_b.id,
            })
        self.assertFalse(self.normal_partner.eh_represented_company_id)

    def test_source_only_sale_user_runs_manager_configured_target_route(self):
        sale = self._source_sale_order()
        self.assertNotIn(self.company_b, self.source_operator.company_ids)
        self._as_source_actor(sale).action_confirm()
        sale.invalidate_recordset()
        purchase = sale.eh_ic_mirror_po_id
        self.assertTrue(purchase)
        self.assertEqual(purchase.company_id, self.company_b)
        self.assertEqual(purchase.create_uid, self.target_service_user)
        self.assertEqual(purchase.eh_ic_origin_so_id, sale)

    def test_source_only_purchase_user_runs_manager_configured_target_route(self):
        purchase = self._source_purchase_order()
        self.assertNotIn(self.company_b, self.source_operator.company_ids)
        self._as_source_actor(purchase).button_confirm()
        purchase.invalidate_recordset()
        sale = purchase.eh_ic_mirror_so_id
        self.assertTrue(sale)
        self.assertEqual(sale.company_id, self.company_b)
        self.assertEqual(sale.create_uid, self.target_service_user)
        self.assertEqual(sale.eh_ic_origin_po_id, purchase)

    def test_source_only_route_fails_closed_without_target_service_user(self):
        self.config_b.sudo().intercompany_user_id = False
        sale = self._source_sale_order()
        with self.assertRaisesRegex(UserError, 'service user'):
            with self.env.cr.savepoint():
                self._as_source_actor(sale).action_confirm()
        sale.invalidate_recordset(['state', 'eh_ic_mirror_po_id'])
        self.assertEqual(sale.state, 'draft')
        self.assertFalse(sale.eh_ic_mirror_po_id)

    def test_cross_company_route_rejects_misscoped_service_identity(self):
        self.config_b.sudo().intercompany_user_id = self.source_operator
        sale = self._source_sale_order()
        with self.assertRaisesRegex(UserError, 'not allowed in target'):
            with self.env.cr.savepoint():
                self._as_source_actor(sale).action_confirm()
        sale.invalidate_recordset(['state', 'eh_ic_mirror_po_id'])
        self.assertEqual(sale.state, 'draft')
        self.assertFalse(sale.eh_ic_mirror_po_id)

    def test_order_provenance_rejects_forgeable_context(self):
        sale = self._sale_order(self.normal_partner)
        purchase = self._purchase_order(self.normal_partner)
        with self.assertRaises(AccessError):
            sale.with_context(
                eh_ic_order_mirror_capability=True,
            ).write({'eh_ic_mirror_po_id': purchase.id})
        with self.assertRaises(AccessError):
            purchase.with_context(
                eh_ic_order_mirror_capability=True,
            ).write({'eh_ic_mirror_so_id': sale.id})

    def test_pair_transition_rejects_forgeable_context(self):
        sale = self._source_sale_order()
        self._as_source_actor(sale).action_confirm()
        sale.invalidate_recordset()
        purchase = sale.eh_ic_mirror_po_id
        forged_sale = sale.sudo().with_context(
            eh_ic_order_pair_transition_capability=True,
        )
        forged_purchase = purchase.sudo().with_context(
            eh_ic_order_pair_transition_capability=True,
        )
        with self.assertRaisesRegex(AccessError, 'server transition'):
            forged_sale._eh_ic_cancel_pair(
                forged_sale, forged_purchase,
            )
        sale.invalidate_recordset(['state'])
        purchase.invalidate_recordset(['state'])
        self.assertEqual((sale.state, purchase.state), ('sale', 'draft'))

    def test_core_derived_lines_are_marked_without_opening_raw_mutation(self):
        sale = self._sale_order(self.partner_b)
        sale.action_confirm()
        purchase = sale.eh_ic_mirror_po_id
        engine_context = {IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY}

        with self.assertRaisesRegex(UserError, 'immutable commercial'):
            self.env['sale.order.line'].create({
                'order_id': sale.id,
                'product_id': self.product.id,
                'product_uom_qty': 1.0,
            })
        expense = self.env['sale.order.line'].with_context(
            **engine_context,
        ).create({
            'order_id': sale.id,
            'product_id': self.product.id,
            'product_uom_qty': 1.0,
            'price_unit': 25.0,
            'is_expense': True,
        })
        self.assertEqual(expense.eh_ic_derived_line_kind, 'expense')

        downpayment_values = {
            'order_id': purchase.id,
            'name': 'Verified purchase down payment',
            'product_id': self.product.id,
            'product_qty': 0.0,
            'product_uom_id': self.product.uom_id.id,
            'price_unit': 30.0,
        }
        if 'is_downpayment' in self.env['purchase.order.line']._fields:
            downpayment_values['is_downpayment'] = True
        else:
            # Purchase down payments are not a core feature on 16/17. The
            # server corridor can still carry an explicit durable marker.
            downpayment_values['eh_ic_derived_line_kind'] = 'downpayment'
        downpayment = self.env['purchase.order.line'].with_context(
            **engine_context,
        ).create(downpayment_values)
        self.assertEqual(
            downpayment.eh_ic_derived_line_kind, 'downpayment',
        )
        # Derived evidence is outside the immutable initial trade scope.
        checked_sale, checked_purchase = sale._eh_ic_lifecycle_pair()
        self.assertEqual((checked_sale, checked_purchase), (sale, purchase))

    def test_ordinary_writes_avoid_full_line_graph_invalidation(self):
        sale = self._sale_order(self.normal_partner)
        purchase = self._purchase_order(self.normal_partner)
        purchase_note_field = (
            'note' if 'note' in purchase._fields else 'notes'
        )
        sale.write({'note': 'ordinary sale edit'})
        sale.order_line.write({'name': 'ordinary sale line edit'})
        purchase.write({purchase_note_field: 'ordinary purchase edit'})
        purchase.order_line.write({'name': 'ordinary purchase line edit'})
        self.assertIn('ordinary sale edit', sale.note)
        self.assertIn(
            'ordinary purchase edit', getattr(purchase, purchase_note_field),
        )

    def test_currency_conversion_keeps_unrounded_unit_precision(self):
        # Avoid target defaults turning this into a tax-inclusion test; this
        # case proves only that conversion waits until extended quantity.
        self.product.product_tmpl_id.sudo().write({
            'taxes_id': [(5, 0, 0)],
            'supplier_taxes_id': [(5, 0, 0)],
        })
        product_price_precision = self.env['decimal.precision'].search([
            ('name', '=', 'Product Price'),
        ], limit=1)
        if product_price_precision:
            product_price_precision.digits = 6
        eur = self.env.ref('base.EUR')
        gbp = self.env.ref('base.GBP')
        rate_date = '2026-05-01'
        cross_company = self.env['res.company'].create({
            'name': 'SoPo Precision B',
            'currency_id': gbp.id,
        })
        self.env.user.company_ids = [(4, cross_company.id)]
        self.company_a.currency_id = eur
        self.env['res.currency.rate'].create({
            'name': rate_date,
            'currency_id': gbp.id,
            'company_id': cross_company.id,
            'rate': 2.0,
        })
        partner = self.env['res.partner'].create({
            'name': 'SoPo precision partner',
            'eh_represented_company_id': cross_company.id,
        })
        self.env['eh.intercompany.config'].sudo().create({
            'company_id': cross_company.id,
            'enabled': True,
        })
        self.company_a.partner_id.with_company(cross_company).write({
            'property_purchase_currency_id': gbp.id,
        })
        pricelist = self.env['product.pricelist'].create({
            'name': 'SoPo precision EUR',
            'currency_id': eur.id,
        })
        sale = self.env['sale.order'].with_company(self.company_a).create({
            'partner_id': partner.id,
            'company_id': self.company_a.id,
            'pricelist_id': pricelist.id,
            'date_order': rate_date,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 1000.0,
                'price_unit': 0.006,
            })],
        })
        expected = eur._convert(
            0.006, gbp, cross_company, rate_date, round=False,
        )
        self.assertNotEqual(expected, gbp.round(expected))
        sale.action_confirm()
        mirror_line = sale.eh_ic_mirror_po_id.order_line
        self.assertAlmostEqual(mirror_line.price_unit, expected, places=8)
        self.assertAlmostEqual(
            sale.eh_ic_mirror_po_id.amount_untaxed,
            gbp.round(expected * 1000.0),
            places=2,
        )

    def test_mirrors_carry_dates_references_shipping_and_no_variant_values(self):
        attribute = self.env['product.attribute'].create({
            'name': 'SoPo engraving',
            'create_variant': 'no_variant',
        })
        attribute_value = self.env['product.attribute.value'].create({
            'name': 'SoPo engraved',
            'attribute_id': attribute.id,
        })
        template = self.env['product.template'].create({
            'name': 'SoPo configured product',
            'attribute_line_ids': [(0, 0, {
                'attribute_id': attribute.id,
                'value_ids': [(6, 0, attribute_value.ids)],
            })],
        })
        product = template.product_variant_id
        no_variant_value = (
            template.attribute_line_ids.product_template_value_ids
        )
        shipping = self.env['res.partner'].create({
            'name': 'SoPo dropship destination',
            'company_id': False,
        })
        order_date = fields.Datetime.to_datetime('2026-07-02 09:00:00')
        commitment = fields.Datetime.to_datetime('2026-07-12 16:00:00')
        sale_line_values = {
            'product_id': product.id,
            'product_uom_qty': 2.0,
            'price_unit': 75.0,
            'product_no_variant_attribute_value_ids': [
                (6, 0, no_variant_value.ids),
            ],
        }
        purchase_fields = self.env['purchase.order']._fields
        company_a_dropship = False
        company_b_dropship = False
        if 'default_location_dest_id_usage' in purchase_fields:
            picking_type = self.env['stock.picking.type'].sudo()
            company_a_dropship = picking_type.search([
                ('company_id', '=', self.company_a.id),
                ('default_location_src_id.usage', '=', 'supplier'),
                ('default_location_dest_id.usage', '=', 'customer'),
            ])
            company_b_dropship = picking_type.search([
                ('company_id', '=', self.company_b.id),
                ('default_location_src_id.usage', '=', 'supplier'),
                ('default_location_dest_id.usage', '=', 'customer'),
            ])
            self.assertEqual(len(company_a_dropship), 1)
            self.assertEqual(len(company_b_dropship), 1)
        sale = self.env['sale.order'].with_company(self.company_a).create({
            'partner_id': self.partner_b.id,
            'partner_shipping_id': shipping.id,
            'company_id': self.company_a.id,
            'date_order': order_date,
            'commitment_date': commitment,
            'client_order_ref': 'CUSTOMER-REF-42',
            'order_line': [(0, 0, sale_line_values)],
        })
        sale.action_confirm()
        purchase = sale.eh_ic_mirror_po_id
        self.assertEqual(purchase.date_order, sale.date_order)
        self.assertEqual(purchase.partner_ref, 'CUSTOMER-REF-42')
        self.assertEqual(purchase.dest_address_id, shipping)
        if company_b_dropship:
            self.assertEqual(purchase.picking_type_id, company_b_dropship)
        self.assertEqual(purchase.order_line.date_planned, commitment)
        if 'product_no_variant_attribute_value_ids' in purchase.order_line._fields:
            self.assertEqual(
                purchase.order_line.product_no_variant_attribute_value_ids,
                no_variant_value,
            )

        purchase_source_values = {
            'partner_id': self.partner_b.id,
            'company_id': self.company_a.id,
            'date_order': order_date,
            'date_planned': commitment,
            'partner_ref': 'VENDOR-REF-24',
            'dest_address_id': shipping.id,
            'order_line': [(0, 0, {
                'product_id': product.id,
                'product_qty': 1.0,
                'product_uom_id': product.uom_id.id,
                'price_unit': 50.0,
                **({
                    'product_no_variant_attribute_value_ids': [
                        (6, 0, no_variant_value.ids),
                    ],
                } if 'product_no_variant_attribute_value_ids'
                      in self.env['purchase.order.line']._fields else {}),
            })],
        }
        if company_a_dropship:
            purchase_source_values['picking_type_id'] = (
                company_a_dropship.id
            )
        purchase_source = self.env['purchase.order'].with_company(
            self.company_a,
        ).create(purchase_source_values)
        purchase_source.button_confirm()
        mirror_sale = purchase_source.eh_ic_mirror_so_id
        self.assertEqual(mirror_sale.date_order, purchase_source.date_order)
        self.assertEqual(mirror_sale.commitment_date, commitment)
        self.assertEqual(mirror_sale.client_order_ref, 'VENDOR-REF-24')
        self.assertEqual(mirror_sale.partner_shipping_id, shipping)
        if 'product_no_variant_attribute_value_ids' in purchase_source.order_line._fields:
            self.assertEqual(
                mirror_sale.order_line.product_no_variant_attribute_value_ids,
                no_variant_value,
            )

    def test_tax_included_and_excluded_prices_preserve_untaxed_trade_value(self):
        country = self.env.ref('base.us')
        tax_group_fields = self.env['account.tax.group']._fields
        source_group_values = {
            'name': 'SoPo A 10%',
            'country_id': country.id,
        }
        target_group_values = {
            'name': 'SoPo B 10%',
            'country_id': country.id,
        }
        if 'company_id' in tax_group_fields:
            source_group_values['company_id'] = self.company_a.id
            target_group_values['company_id'] = self.company_b.id
        source_tax_group = self.env['account.tax.group'].with_company(
            self.company_a,
        ).create(source_group_values)
        target_tax_group = self.env['account.tax.group'].with_company(
            self.company_b,
        ).create(target_group_values)
        source_sale_tax = self.env['account.tax'].with_company(
            self.company_a,
        ).create({
            'name': 'SoPo A included sale 10',
            'amount': 10.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'price_include_override': 'tax_included',
            'tax_group_id': source_tax_group.id,
            'country_id': country.id,
            'company_id': self.company_a.id,
        })
        target_purchase_tax = self.env['account.tax'].with_company(
            self.company_b,
        ).create({
            'name': 'SoPo B excluded purchase 10',
            'amount': 10.0,
            'amount_type': 'percent',
            'type_tax_use': 'purchase',
            'price_include_override': 'tax_excluded',
            'tax_group_id': target_tax_group.id,
            'country_id': country.id,
            'company_id': self.company_b.id,
        })
        target_sale_tax = self.env['account.tax'].with_company(
            self.company_b,
        ).create({
            'name': 'SoPo B included sale 10',
            'amount': 10.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'price_include_override': 'tax_included',
            'tax_group_id': target_tax_group.id,
            'country_id': country.id,
            'company_id': self.company_b.id,
        })
        product = self.env['product.product'].create({
            'name': 'SoPo tax-semantic product',
            'list_price': 100.0,
            'standard_price': 100.0,
        })
        product.product_tmpl_id.sudo().write({
            'taxes_id': [(6, 0, (source_sale_tax | target_sale_tax).ids)],
            'supplier_taxes_id': [(6, 0, target_purchase_tax.ids)],
        })
        sale_tax_key = (
            'tax_ids'
            if 'tax_ids' in self.env['sale.order.line']._fields
            else 'tax_id'
        )
        purchase_tax_key = (
            'tax_ids'
            if 'tax_ids' in self.env['purchase.order.line']._fields
            else 'taxes_id'
        )
        sale = self.env['sale.order'].with_company(self.company_a).create({
            'partner_id': self.partner_b.id,
            'company_id': self.company_a.id,
            'order_line': [(0, 0, {
                'product_id': product.id,
                'product_uom_qty': 2.0,
                'price_unit': 110.0,
                sale_tax_key: [(6, 0, source_sale_tax.ids)],
            })],
        })
        sale.action_confirm()
        purchase = sale.eh_ic_mirror_po_id
        self.assertAlmostEqual(purchase.order_line.price_unit, 100.0, places=6)
        self.assertEqual(
            getattr(purchase.order_line, purchase_tax_key),
            target_purchase_tax,
        )
        self.assertAlmostEqual(purchase.amount_untaxed, 200.0, places=2)
        self.assertAlmostEqual(purchase.amount_total, 220.0, places=2)
        self.assertIn(
            'SoPo A included sale 10',
            purchase.order_line.eh_ic_source_tax_summary,
        )

        purchase_source = self.env['purchase.order'].with_company(
            self.company_a,
        ).create({
            'partner_id': self.partner_b.id,
            'company_id': self.company_a.id,
            'order_line': [(0, 0, {
                'product_id': product.id,
                'product_qty': 3.0,
                'product_uom_id': product.uom_id.id,
                'price_unit': 100.0,
                purchase_tax_key: [(6, 0, [])],
            })],
        })
        purchase_source.button_confirm()
        mirror_sale = purchase_source.eh_ic_mirror_so_id
        self.assertEqual(
            getattr(mirror_sale.order_line, sale_tax_key), target_sale_tax,
        )
        self.assertAlmostEqual(
            mirror_sale.order_line.price_unit, 110.0, places=6,
        )
        self.assertAlmostEqual(mirror_sale.amount_untaxed, 300.0, places=2)
        self.assertAlmostEqual(mirror_sale.amount_total, 330.0, places=2)

    def test_company_restricted_products_resolve_one_target_sku(self):
        source_product = self.env['product.product'].with_company(
            self.company_a,
        ).create({
            'name': 'SoPo source restricted SKU',
            'default_code': 'SOPO-COMPANY-SKU',
            'company_id': self.company_a.id,
        })
        target_product = self.env['product.product'].sudo().with_company(
            self.company_b,
        ).create({
            'name': 'SoPo target restricted SKU',
            'default_code': 'SOPO-COMPANY-SKU',
            'company_id': self.company_b.id,
        })
        sale = self.env['sale.order'].with_company(self.company_a).create({
            'partner_id': self.partner_b.id,
            'company_id': self.company_a.id,
            'order_line': [(0, 0, {
                'product_id': source_product.id,
                'product_uom_qty': 2.0,
                'price_unit': 60.0,
            })],
        })
        sale.action_confirm()
        self.assertEqual(
            sale.eh_ic_mirror_po_id.order_line.product_id,
            target_product,
        )
        verified_sale, verified_purchase = sale._eh_ic_lifecycle_pair()
        self.assertEqual(
            (verified_sale, verified_purchase),
            (sale, sale.eh_ic_mirror_po_id),
        )

        unmapped = self.env['product.product'].with_company(
            self.company_a,
        ).create({
            'name': 'SoPo restricted product without a reference',
            'company_id': self.company_a.id,
        })
        blocked = self.env['sale.order'].with_company(self.company_a).create({
            'partner_id': self.partner_b.id,
            'company_id': self.company_a.id,
            'order_line': [(0, 0, {
                'product_id': unmapped.id,
                'product_uom_qty': 1.0,
                'price_unit': 20.0,
            })],
        })
        with self.assertRaisesRegex(UserError, 'internal reference'):
            with self.env.cr.savepoint():
                blocked.action_confirm()
        blocked.invalidate_recordset(['state', 'eh_ic_mirror_po_id'])
        self.assertEqual(blocked.state, 'draft')
        self.assertFalse(blocked.eh_ic_mirror_po_id)

    def test_non_intercompany_purchase_approve_keeps_core_semantics(self):
        purchase = self._purchase_order(self.normal_partner)
        result = purchase.button_approve()
        self.assertEqual(result, {})
        self.assertEqual(purchase.state, 'purchase')

    def test_capabilities_are_serialisable_and_dead_helper_is_absent(self):
        payload = {
            IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
            IC_PAIR_TRANSITION_CONTEXT: IC_PAIR_TRANSITION_CAPABILITY,
        }
        self.assertEqual(json.loads(json.dumps(payload)), payload)
        self.assertTrue(all(isinstance(value, str) for value in payload.values()))
        mixin_source = (
            Path(__file__).resolve().parents[1]
            / 'models' / 'intercompany_order_mixin.py'
        ).read_text(encoding='utf-8')
        self.assertNotIn('def _eh_ic_engine_context', mixin_source)

    def test_public_name_and_listing_version_policy(self):
        module_root = Path(__file__).resolve().parents[1]
        manifest = ast.literal_eval(
            (module_root / '__manifest__.py').read_text(encoding='utf-8')
        )
        listing = (
            module_root / 'static' / 'description' / 'index.html'
        ).read_text(encoding='utf-8')
        self.assertEqual(manifest['name'], 'Intercompany SO/PO')
        self.assertLessEqual(len(manifest['name']), 25)
        self.assertIn('>Intercompany SO/PO<', listing)
        self.assertEqual(listing.count(manifest['version']), 2)
        self.assertIn('sealed commercial terms', listing)
        self.assertNotIn('review lines, pricing, and quantities', listing)
        self.assertNotIn('mirrors it back to a customer invoice', listing)
        self.assertNotIn('otherwise sudo() is used', listing)
