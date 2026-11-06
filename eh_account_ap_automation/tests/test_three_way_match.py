# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
3 way match tests: PO discovery, qty / price tolerance, over receipt,
override flow.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhApTestCase


@tagged('eh_account_ap_automation', 'integration', 'post_install', '-at_install')
class TestThreeWayMatch(EhApTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager',
        )
        cls.po = cls._create_purchase_order([
            (cls.product_a, 10, 12.50),
            (cls.product_b, 5, 25.00),
        ])
        cls._confirm_receipt(cls.po)

    def _build_intake(self, lines, purchase_order=None):
        total = 0.0
        for product, qty, price in lines:
            po_line = (
                purchase_order.order_line.filtered(
                    lambda candidate: candidate.product_id == product,
                )[:1]
                if purchase_order else self.env['purchase.order.line']
            )
            taxes = (
                po_line.tax_ids if po_line
                else product.supplier_taxes_id.filtered(
                    lambda tax: tax.company_id == self.company,
                )
            )
            total += taxes.compute_all(
                price,
                currency=self.company.currency_id,
                quantity=qty,
                product=product,
                partner=self.partner_a,
            )['total_included']
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'purchase_order_id': (
                purchase_order.id if purchase_order else False
            ),
            'vendor_reference': 'TEST-%s' % self.env['ir.sequence'].next_by_code(
                'eh.ap.intake',
            ),
            'extracted_total': total,
            'state': 'parsed',
        })
        for product, qty, price in lines:
            self.env['eh.ap.intake.line'].create({
                'intake_id': intake.id,
                'product_id': product.id,
                'product_code': product.default_code,
                'invoice_qty': qty,
                'invoice_price': price,
                'subtotal': qty * price,
            })
        return intake

    # ---- happy path ----

    def test_match_within_tolerance(self):
        intake = self._build_intake(
            [(self.product_a, 10, 12.50), (self.product_b, 5, 25.00)],
            purchase_order=self.po,
        )
        intake.action_match()
        self.assertEqual(intake.state, 'matched')
        for line in intake.line_ids:
            self.assertEqual(line.match_status, 'ok')
            self.assertTrue(line.purchase_order_line_id)
            self.assertEqual(line.received_qty, line.po_qty)

    def test_match_handles_foreign_currency_bill(self):
        """A bill raised in a different currency from the PO must compare
        prices after conversion, not flag every line as a price exception.
        The PO line is 12.50 in company currency; the bill is in TAPX at 2
        per unit, so 25.00 TAPX is the same price and must match."""
        fx = self.env['res.currency'].create({
            'name': 'TAPX', 'symbol': 'X', 'rounding': 0.01, 'active': True,
        })
        fx.rate_ids.unlink()
        self.env['res.currency.rate'].create({
            'currency_id': fx.id,
            'name': '2026-06-15',
            'rate': 2.0,  # 2 TAPX per 1 unit of company currency
            'company_id': self.company.id,
        })
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'purchase_order_id': self.po.id,
            'currency_id': fx.id,
            'bill_date': '2026-06-15',
            'state': 'parsed',
        })
        self.env['eh.ap.intake.line'].create({
            'intake_id': intake.id,
            'product_id': self.product_a.id,
            'product_code': self.product_a.default_code,
            'invoice_qty': 10,
            'invoice_price': 25.00,  # 12.50 PO price x 2 = same price in TAPX
            'subtotal': 250.00,
        })
        intake.action_match()
        line = intake.line_ids
        self.assertEqual(line.match_status, 'ok')
        # The PO price is shown converted into the bill currency.
        self.assertAlmostEqual(line.po_price, 25.00, places=2)
        self.assertAlmostEqual(line.price_diff, 0.0, places=2)

    def test_post_creates_in_invoice(self):
        intake = self._build_intake(
            [(self.product_a, 10, 12.50)],
            purchase_order=self.po,
        )
        intake.action_match()
        intake.action_post()
        self.assertEqual(intake.state, 'posted')
        self.assertTrue(intake.move_id)
        self.assertEqual(intake.move_id.move_type, 'in_invoice')
        self.assertEqual(intake.move_id.partner_id, self.partner_a)
        self.assertEqual(intake.move_id.state, 'posted')
        self.assertTrue(intake.move_id.eh_sealed)
        self.assertEqual(intake.move_id.eh_ap_intake_id, intake)

    # ---- exception paths ----

    def test_qty_exception_routes_to_exception_state(self):
        # Default profile: 2% qty tolerance, 0% over-receipt. Bill less
        # than received but more than tolerance allows: 8 vs 10 received = 20% delta.
        intake = self._build_intake(
            [(self.product_a, 8, 12.50)],
            purchase_order=self.po,
        )
        intake.action_match()
        self.assertEqual(intake.state, 'exception')
        line = intake.line_ids
        self.assertEqual(line.match_status, 'qty_exception')
        self.assertGreater(abs(line.qty_diff), 0)

    def test_price_exception(self):
        # Default profile: 1% price tolerance + 5.00 amount tolerance.
        # PO price 12.50, bill 13.50 = 8% delta, line value diff = 10.00.
        intake = self._build_intake(
            [(self.product_a, 10, 13.50)],
            purchase_order=self.po,
        )
        intake.action_match()
        self.assertEqual(intake.state, 'exception')
        line = intake.line_ids
        self.assertEqual(line.match_status, 'price_exception')

    def test_over_received_when_no_tolerance(self):
        # PO qty 10, no over receipt allowed. Bill 12 = over received.
        intake = self._build_intake(
            [(self.product_a, 12, 12.50)],
            purchase_order=self.po,
        )
        intake.action_match()
        line = intake.line_ids
        # Either over_received or qty_exception depending on which check
        # fires first; both indicate exception state at the intake level.
        self.assertIn(
            line.match_status,
            ('over_received', 'qty_exception'),
        )
        self.assertEqual(intake.state, 'exception')

    def test_no_match_when_product_not_on_po(self):
        Product = self.env['product.product']
        product_c = Product.create({
            'name': 'Unrelated Product',
            'default_code': 'UNREL-001',
            'type': 'consu',
        })
        intake = self._build_intake(
            [(product_c, 1, 10.0)],
            purchase_order=self.po,
        )
        intake.action_match()
        line = intake.line_ids
        self.assertEqual(line.match_status, 'no_match')

    # ---- override ----

    def test_override_clears_exception(self):
        intake = self._build_intake(
            [(self.product_a, 11, 12.50)],
            purchase_order=self.po,
        )
        intake.action_match()
        self.assertEqual(intake.state, 'exception')
        line = intake.line_ids
        line.override_reason = 'Approved variance'
        line.action_override()
        self.assertEqual(line.match_status, 'overridden')
        self.assertEqual(intake.state, 'matched')

    def test_override_requires_reason(self):
        intake = self._build_intake(
            [(self.product_a, 11, 12.50)],
            purchase_order=self.po,
        )
        intake.action_match()
        line = intake.line_ids
        with self.assertRaises(UserError):
            line.action_override()

    def test_post_blocked_when_exception_present(self):
        intake = self._build_intake(
            [(self.product_a, 11, 12.50)],
            purchase_order=self.po,
        )
        intake.action_match()
        with self.assertRaises(UserError):
            intake.action_post()

    # ---- partner profile override ----

    def test_partner_profile_loosens_tolerance(self):
        loose = self.env['eh.ap.tolerance.profile'].create({
            'name': 'Loose for Vendor A',
            'qty_tolerance_pct': 25.0,
            'price_tolerance_pct': 25.0,
            'amount_tolerance': 0.0,
            'over_receipt_pct': 25.0,
        })
        self.partner_a.eh_ap_tolerance_profile_id = loose
        intake = self._build_intake(
            [(self.product_a, 11, 12.50)],
            purchase_order=self.po,
        )
        intake.action_match()
        self.assertEqual(intake.line_ids.match_status, 'ok')
        self.assertEqual(intake.state, 'matched')
