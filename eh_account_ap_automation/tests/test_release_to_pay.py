# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Release-to-pay governance: purchase-method bill control + manual hold."""

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_ap_automation.tests.common import EhApTestCase


@tagged('eh_account_ap_automation', 'integration', 'post_install',
        '-at_install')
class TestReleaseToPay(EhApTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = cls.env['res.users'].create({
            'name': 'AP Release Manager',
            'login': 'ap_release_manager',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_manager').id,
            ])],
        })

    def _bill_from_po(self, po):
        po.action_create_invoice()
        bill = po.invoice_ids[:1]
        bill.invoice_date = '2026-03-15'
        return bill

    def test_draft_is_not_ready(self):
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice', 'partner_id': self.partner_a.id})
        self.assertEqual(bill.eh_release_to_pay, 'not_ready')

    def test_received_method_within_receipt_is_released(self):
        self.product_a.purchase_method = 'receive'
        po = self._create_purchase_order([(self.product_a, 10, 100.0)])
        self._confirm_receipt(po)  # full 10 received
        bill = self._bill_from_po(po)
        bill.action_post()
        bill._eh_stamp_verified_seal()
        self.assertEqual(bill.eh_release_to_pay, 'released')

    def test_received_method_overbill_is_exception(self):
        self.product_a.purchase_method = 'receive'
        po = self._create_purchase_order([(self.product_a, 10, 100.0)])
        self._confirm_receipt(po, qty_overrides={po.order_line.id: 4})
        bill = self._bill_from_po(po)
        # Bill the full ordered 10 though only 4 were received.
        bill.invoice_line_ids.filtered(
            lambda l: l.purchase_line_id).quantity = 10
        bill.action_post()
        self.assertEqual(bill.eh_release_to_pay, 'exception')

    def test_ordered_method_bills_without_receipt(self):
        self.product_a.purchase_method = 'purchase'
        po = self._create_purchase_order([(self.product_a, 10, 100.0)])
        # No receipt at all; ordered-control allows billing the order.
        bill = self._bill_from_po(po)
        bill.action_post()
        self.assertEqual(bill.eh_release_to_pay, 'released')

    def test_manual_hold_and_release(self):
        self.product_a.purchase_method = 'purchase'
        po = self._create_purchase_order([(self.product_a, 5, 50.0)])
        bill = self._bill_from_po(po)
        bill.action_post()
        bill._eh_stamp_verified_seal()
        self.assertEqual(bill.eh_release_to_pay, 'released')

        bill.with_user(self.manager).eh_payment_action_reason = (
            'Dispute under review'
        )
        bill.with_user(self.manager).action_eh_hold_payment()
        self.assertEqual(bill.eh_release_to_pay, 'hold')

        bill.with_user(self.manager).eh_payment_action_reason = 'Dispute resolved'
        bill.with_user(self.manager).action_eh_release_payment()
        self.assertEqual(bill.eh_release_to_pay, 'released')

    def test_manager_waiver_is_exact_and_stale_evidence_blocks_again(self):
        self.product_a.purchase_method = 'receive'
        po = self._create_purchase_order([(self.product_a, 10, 100.0)])
        self._confirm_receipt(po, qty_overrides={po.order_line.id: 4})
        bill = self._bill_from_po(po)
        bill.invoice_line_ids.filtered(
            lambda line: line.purchase_line_id,
        ).quantity = 10
        bill.action_post()
        self.assertEqual(bill.eh_release_to_pay, 'exception')
        with self.assertRaises(UserError):
            bill._eh_assert_released_for_payment()

        bill.with_user(self.manager).eh_payment_action_reason = (
            'Contract milestone permits billing before final receipt'
        )
        bill.with_user(self.manager).action_eh_waive_billing_exception()
        self.assertEqual(bill.eh_release_to_pay, 'exception_waived')
        self.assertTrue(bill.eh_exception_waiver_fingerprint)
        self.assertEqual(
            bill.eh_payment_hold_log_ids[:1].action,
            'exception_waive',
        )
        bill._eh_assert_released_for_payment()

        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE purchase_order_line "
            "SET product_qty = product_qty + 1, write_date = NOW() "
            "WHERE id = %s",
            [po.order_line.id],
        )
        po.order_line.invalidate_recordset(['product_qty', 'write_date'])
        bill.sudo().with_context(
            eh_ap_payment_control_internal=True,
        )._compute_eh_release_to_pay()
        bill.invalidate_recordset(['eh_release_to_pay'])
        self.assertEqual(bill.eh_release_to_pay, 'exception')
        with self.assertRaises(UserError):
            bill._eh_assert_released_for_payment()

    def test_posted_vendor_refund_never_reports_released_to_pay(self):
        refund = self.env['account.move'].create({
            'move_type': 'in_refund',
            'partner_id': self.partner_a.id,
            'invoice_date': fields.Date.today(),
            'invoice_line_ids': [Command.create({
                'product_id': self.product_a.id,
                'name': 'Supplier refund',
                'quantity': 1.0,
                'price_unit': 10.0,
                'tax_ids': [Command.clear()],
            })],
        })
        refund.action_post()
        self.assertEqual(refund.eh_release_to_pay, 'not_ready')
