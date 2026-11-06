# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Duplicate-bill detection tests.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhApTestCase


@tagged('eh_account_ap_automation', 'integration', 'post_install', '-at_install')
class TestDuplicateDetection(EhApTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mgr_user = cls.env['res.users'].create({
            'name': 'AP Manager',
            'login': 'ap_mgr_dup_test',
            'email': 'ap_mgr_dup_test@example.com',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_manager').id,
            ])],
        })

    def _make_intake(self, ref, total, bill_date='2026-04-30'):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'company_id': self.company.id,
            'currency_id': self.company.currency_id.id,
            'vendor_reference': ref,
            'extracted_total': total,
            'bill_date': bill_date,
        })
        intake.line_ids = [(0, 0, {
            'product_code': 'WIDGET-001',
            'product_id': self.product_a.id,
            'invoice_qty': 1.0,
            'invoice_price': total,
            'subtotal': total,
        })]
        intake._eh_capture_source_evidence()
        return intake

    def test_duplicate_intake_detected(self):
        first = self._make_intake('INV-100', 250.0)
        first.write({'state': 'matched'})
        second = self._make_intake('INV-100', 250.0)
        second._eh_check_duplicate()
        self.assertEqual(second.duplicate_intake_id, first)

    def test_no_duplicate_when_amount_differs(self):
        first = self._make_intake('INV-101', 250.0)
        first.write({'state': 'matched'})
        second = self._make_intake('INV-101', 251.0)
        second._eh_check_duplicate()
        self.assertFalse(second.duplicate_intake_id)

    def test_no_duplicate_when_partner_differs(self):
        first = self._make_intake('INV-102', 100.0)
        first.write({'state': 'matched'})
        other_vendor = self.env['res.partner'].create({
            'name': 'Other Vendor',
            'is_company': True,
        })
        second = self.env['eh.ap.intake'].create({
            'partner_id': other_vendor.id,
            'vendor_reference': 'INV-102',
            'extracted_total': 100.0,
            'bill_date': '2026-04-30',
        })
        second.line_ids = [(0, 0, {
            'product_code': 'WIDGET-001',
            'product_id': self.product_a.id,
            'invoice_qty': 1.0,
            'invoice_price': 100.0,
            'subtotal': 100.0,
        })]
        second._eh_check_duplicate()
        self.assertFalse(second.duplicate_intake_id)

    def test_post_blocks_when_duplicate_unresolved(self):
        first = self._make_intake('INV-200', 333.0)
        first.write({'state': 'matched'})
        second = self._make_intake('INV-200', 333.0)
        second.write({'state': 'matched'})
        second._eh_check_duplicate()
        self.assertEqual(second.duplicate_intake_id, first)
        with self.assertRaises(UserError):
            second.with_user(self.mgr_user).action_post()

    def test_override_requires_reason(self):
        first = self._make_intake('INV-300', 444.0)
        first.write({'state': 'matched'})
        second = self._make_intake('INV-300', 444.0)
        second.write({'state': 'matched'})
        second._eh_check_duplicate()
        # Without a reason the override must raise UserError. Run under
        # mgr_user to exercise the manager-only ACL check too.
        with self.assertRaises(UserError):
            second.with_user(self.mgr_user).action_override_duplicate()
        second.duplicate_override_request_reason = "Same ref, different bill"
        second.with_user(self.mgr_user).action_override_duplicate()
        # Override does not clear flag, but unblocks post via the
        # captured reason.
        self.assertEqual(second.duplicate_intake_id, first)
        self.assertTrue(second.duplicate_override_reason)
        self.assertTrue(second.duplicate_override_log_id)
