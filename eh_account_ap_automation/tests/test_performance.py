# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Performance regression guards for the AP automation hot paths.
"""

from odoo.tests import tagged

from .common import EhApTestCase


@tagged('eh_account_ap_automation', 'performance',
        'post_install', '-at_install')
class TestApPerformance(EhApTestCase):

    SAMPLE = """
ACME Supplies
Invoice: PERF-INV-0001
WIDGET-001 10 12.50
GADGET-002 5 25.00
Total: 250.00
"""

    def test_query_budget_parse(self):
        """Parsing a small bill must fit under a fixed query budget."""
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'raw_text': self.SAMPLE,
        })
        with self.assertQueryCount(__system__=220):
            intake.action_parse()
        self.assertEqual(intake.state, 'parsed')

    def test_query_budget_match_two_lines(self):
        """3 way match across 2 lines must stay under budget."""
        po = self._create_purchase_order([
            (self.product_a, 10, 12.50),
            (self.product_b, 5, 25.00),
        ])
        self._confirm_receipt(po)
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'purchase_order_id': po.id,
            'state': 'parsed',
        })
        for product, qty, price in [
            (self.product_a, 10, 12.50),
            (self.product_b, 5, 25.00),
        ]:
            self.env['eh.ap.intake.line'].create({
                'intake_id': intake.id,
                'product_id': product.id,
                'product_code': product.default_code,
                'invoice_qty': qty,
                'invoice_price': price,
                'subtotal': qty * price,
            })
        with self.assertQueryCount(__system__=650):
            intake.action_match()
        self.assertEqual(intake.state, 'matched')
