# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Security, provenance, concurrency-evidence and exact-total regressions."""

import base64

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from .common import EhApTestCase


@tagged('eh_account_ap_automation', 'integration', 'post_install', '-at_install')
class TestApHardening(EhApTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager',
        )
        cls.manager = cls.env['res.users'].create({
            'name': 'AP Hardening Manager',
            'login': 'ap_hardening_manager',
            'email': 'ap_hardening_manager@example.com',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('base.group_partner_manager').id,
                cls.env.ref('eh_account_base.group_eh_manager').id,
            ])],
        })
        cls.user = cls.env['res.users'].create({
            'name': 'AP Hardening User',
            'login': 'ap_hardening_user',
            'email': 'ap_hardening_user@example.com',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        cls.auditor = cls.env['res.users'].create({
            'name': 'AP Hardening Auditor',
            'login': 'ap_hardening_auditor',
            'email': 'ap_hardening_auditor@example.com',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_auditor').id,
            ])],
        })
        cls.po = cls._create_purchase_order([(cls.product_a, 10, 12.50)])
        cls._confirm_receipt(cls.po)

    def test_mail_gateway_accepts_core_msg_dict_keyword(self):
        intake = self.env['eh.ap.intake'].message_new(msg_dict={
            'subject': 'Keyword-compatible vendor bill',
            'body': 'Invoice total 125.00',
            'email_from': 'unknown.vendor@example.com',
        })
        self.assertEqual(
            intake.vendor_reference,
            'Keyword-compatible vendor bill',
        )
        self.assertIn('Invoice total 125.00', intake.raw_text)

    def _make_intake(self, reference='HARD-001', qty=10.0, price=12.50,
                     captured_total=None):
        captured_total = (
            qty * price if captured_total is None else captured_total
        )
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'purchase_order_id': self.po.id,
            'vendor_reference': reference,
            'bill_date': fields.Date.today(),
            'extracted_total': captured_total,
            'raw_text': 'Invoice: %s\nTotal: %.2f' % (
                reference, captured_total,
            ),
            'state': 'parsed',
        })
        line = self.env['eh.ap.intake.line'].create({
            'intake_id': intake.id,
            'product_id': self.product_a.id,
            'product_code': self.product_a.default_code,
            'invoice_qty': qty,
            'invoice_price': price,
            'subtotal': qty * price,
        })
        intake.action_match()
        return intake, line

    def test_intake_audit_fields_are_server_owned(self):
        Intake = self.env['eh.ap.intake'].with_user(self.user)
        with self.assertRaises(AccessError):
            Intake.create({
                'partner_id': self.partner_a.id,
                'parsed_at': fields.Datetime.now(),
            })
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
        })
        with self.assertRaises(AccessError):
            intake.with_user(self.user).write({
                'posted_by_id': self.user.id,
            })

    def test_matched_bill_preserves_purchase_order_uom(self):
        pack = self.env.ref(
            'uom.product_uom_pack_6', raise_if_not_found=False,
        ) or self.env.ref('uom.product_uom_dozen')
        po = self.env['purchase.order'].create({
            'partner_id': self.partner_a.id,
            'company_id': self.company.id,
            'order_line': [Command.create({
                'product_id': self.product_a.id,
                'name': self.product_a.display_name,
                'product_qty': 2.0,
                'price_unit': 60.0,
                'product_uom_id': pack.id,
            })],
        })
        po.button_confirm()
        self._confirm_receipt(po)
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'purchase_order_id': po.id,
            'vendor_reference': 'HARD-UOM',
            'bill_date': fields.Date.today(),
            'extracted_total': po.amount_total,
            'raw_text': 'Invoice: HARD-UOM\nTotal: %s' % po.amount_total,
            'state': 'parsed',
        })
        line = self.env['eh.ap.intake.line'].create({
            'intake_id': intake.id,
            'product_id': self.product_a.id,
            'product_code': self.product_a.default_code,
            'invoice_qty': 2.0,
            'invoice_price': 60.0,
            'subtotal': 120.0,
        })
        intake.action_match()
        self.assertEqual(line.purchase_order_line_id, po.order_line)
        self.assertEqual(
            line._build_invoice_line_vals()['product_uom_id'],
            pack.id,
        )
        intake.with_user(self.manager).action_post()
        bill_line = intake.move_id.invoice_line_ids.filtered(
            lambda candidate: candidate.purchase_line_id == po.order_line
        )
        self.assertEqual(bill_line.product_uom_id, pack)
        self.assertEqual(bill_line.quantity, 2.0)

    def test_line_match_fields_are_server_owned(self):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
        })
        with self.assertRaises(AccessError):
            self.env['eh.ap.intake.line'].with_user(self.user).create({
                'intake_id': intake.id,
                'invoice_qty': 1.0,
                'invoice_price': 1.0,
                'subtotal': 1.0,
                'purchase_order_line_id': self.po.order_line.id,
            })

    def test_release_status_and_provenance_are_server_owned(self):
        self.product_a.purchase_method = 'purchase'
        po = self._create_purchase_order([(self.product_a, 1, 12.50)])
        po.action_create_invoice()
        bill = po.invoice_ids[:1]
        with self.assertRaises(AccessError):
            bill.with_user(self.user).write({
                'eh_release_to_pay': 'released',
            })
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
        })
        with self.assertRaises(AccessError):
            bill.with_user(self.user).write({'eh_ap_intake_id': intake.id})

    def test_line_company_is_server_owned_and_matches_intake(self):
        company_b = self.env['res.company'].create({
            'name': 'AP Line Isolation B',
            'currency_id': self.company.currency_id.id,
        })
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
        })
        line = self.env['eh.ap.intake.line'].create({
            'intake_id': intake.id,
            'product_id': self.product_a.id,
            'invoice_qty': 1.0,
            'invoice_price': 1.0,
            'subtotal': 1.0,
        })
        with self.assertRaises(AccessError):
            line.with_user(self.user).write({'company_id': company_b.id})
        with self.assertRaises(AccessError):
            self.env['eh.ap.intake.line'].with_user(self.user).create({
                'intake_id': intake.id,
                'company_id': company_b.id,
                'invoice_qty': 1.0,
                'invoice_price': 1.0,
                'subtotal': 1.0,
            })
        self.assertEqual(line.company_id, intake.company_id)

    def test_same_po_siblings_consume_receipt_once(self):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'purchase_order_id': self.po.id,
            'vendor_reference': 'HARD-SIBLING',
            'bill_date': fields.Date.today(),
            'extracted_total': 250.0,
            'raw_text': 'Invoice: HARD-SIBLING\nTotal: 250.00',
            'state': 'parsed',
        })
        lines = self.env['eh.ap.intake.line'].create([
            {
                'intake_id': intake.id,
                'product_id': self.product_a.id,
                'sequence': sequence,
                'invoice_qty': 10.0,
                'invoice_price': 12.50,
                'subtotal': 125.0,
            }
            for sequence in (10, 20)
        ])
        intake.action_match()
        lines.invalidate_recordset(['match_status'])
        self.assertEqual(lines[0].match_status, 'ok')
        self.assertIn(lines[1].match_status, ('qty_exception', 'over_received'))
        self.assertEqual(intake.state, 'exception')

    def test_po_discount_matches_and_posts_effective_price_once(self):
        po = self._create_purchase_order([(self.product_a, 2, 100.0)])
        if 'discount' not in po.order_line._fields:
            self.skipTest('purchase-line discounts are unavailable')
        po.order_line.write({
            'discount': 20.0,
            (
                'tax_ids'
                if 'tax_ids' in po.order_line._fields
                else 'taxes_id'
            ): [Command.clear()],
        })
        self._confirm_receipt(po)
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'purchase_order_id': po.id,
            'vendor_reference': 'HARD-DISCOUNT',
            'bill_date': fields.Date.today(),
            'extracted_total': 160.0,
            'raw_text': 'Invoice: HARD-DISCOUNT\nTotal: 160.00',
            'state': 'parsed',
        })
        line = self.env['eh.ap.intake.line'].create({
            'intake_id': intake.id,
            'product_id': self.product_a.id,
            'invoice_qty': 2.0,
            # Captured line prices are effective: the extractor back-solves
            # line_total / quantity when a source also exposes a gross price.
            'invoice_price': 80.0,
            'subtotal': 160.0,
        })
        intake.action_match()
        self.assertEqual(line.match_status, 'ok')
        self.assertEqual(line.po_price, 80.0)
        intake.with_user(self.manager).action_post()
        bill_line = intake.move_id.invoice_line_ids.filtered(
            lambda line: line.product_id == self.product_a,
        )
        self.assertEqual(len(bill_line), 1)
        self.assertEqual(bill_line.price_unit, 80.0)
        self.assertEqual(bill_line.discount, 0.0)
        self.assertEqual(intake.move_id.amount_total, 160.0)

    def test_po_discount_rejects_undiscounted_invoice_price(self):
        po = self._create_purchase_order([(self.product_a, 2, 100.0)])
        if 'discount' not in po.order_line._fields:
            self.skipTest('purchase-line discounts are unavailable')
        po.order_line.write({
            'discount': 20.0,
            (
                'tax_ids'
                if 'tax_ids' in po.order_line._fields
                else 'taxes_id'
            ): [Command.clear()],
        })
        self._confirm_receipt(po)
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'purchase_order_id': po.id,
            'vendor_reference': 'HARD-GROSS-DISCOUNT',
            'bill_date': fields.Date.today(),
            'extracted_total': 200.0,
            'raw_text': 'Invoice: HARD-GROSS-DISCOUNT\nTotal: 200.00',
            'state': 'parsed',
        })
        line = self.env['eh.ap.intake.line'].create({
            'intake_id': intake.id,
            'product_id': self.product_a.id,
            'invoice_qty': 2.0,
            'invoice_price': 100.0,
            'subtotal': 200.0,
        })
        intake.action_match()
        self.assertEqual(line.po_price, 80.0)
        self.assertEqual(line.price_diff, 20.0)
        self.assertEqual(line.match_status, 'price_exception')

    def test_fx_rate_drift_invalidates_match_evidence(self):
        eur = self.env.ref('base.EUR')
        eur.active = True
        bill_date = fields.Date.from_string('2026-01-15')
        Rate = self.env['res.currency.rate']
        root_company = self.company.root_id
        rate = Rate.search([
            ('currency_id', '=', eur.id),
            ('company_id', '=', root_company.id),
            ('name', '=', bill_date),
        ], limit=1)
        if rate:
            rate.rate = 0.8
        else:
            rate = Rate.create({
                'currency_id': eur.id,
                'company_id': root_company.id,
                'name': bill_date,
                'rate': 0.8,
            })
        po = self.env['purchase.order'].create({
            'partner_id': self.partner_a.id,
            'company_id': self.company.id,
            'currency_id': eur.id,
            'order_line': [Command.create({
                'product_id': self.product_a.id,
                'name': self.product_a.display_name,
                'product_qty': 1.0,
                'price_unit': 100.0,
                'product_uom_id': self.product_a.uom_id.id,
            })],
        })
        po.button_confirm()
        self._confirm_receipt(po)
        converted_price = eur._convert(
            100.0, self.company.currency_id, self.company, bill_date,
            round=False,
        )
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'purchase_order_id': po.id,
            'vendor_reference': 'HARD-FX',
            'bill_date': bill_date,
            'currency_id': self.company.currency_id.id,
            'extracted_total': converted_price,
            'raw_text': 'Invoice: HARD-FX\nTotal: %.2f' % converted_price,
            'state': 'parsed',
        })
        line = self.env['eh.ap.intake.line'].create({
            'intake_id': intake.id,
            'product_id': self.product_a.id,
            'invoice_qty': 1.0,
            'invoice_price': converted_price,
            'subtotal': converted_price,
        })
        intake.action_match()
        self.assertEqual(line.match_status, 'ok')
        old_fingerprint = line.match_evidence_fingerprint
        rate.rate = 0.9
        self.assertEqual(line.match_evidence_fingerprint, old_fingerprint)
        with self.assertRaises(UserError):
            intake.with_user(self.manager).action_post()

    def test_hold_blocks_register_payment_sources_and_reconciliation(self):
        self.product_a.purchase_method = 'purchase'
        po = self._create_purchase_order([(self.product_a, 1, 12.50)])
        po.action_create_invoice()
        bill = po.invoice_ids[:1]
        bill.invoice_date = fields.Date.today()
        bill.action_post()
        bill.with_user(self.manager).eh_payment_action_reason = 'Dispute'
        bill.with_user(self.manager).action_eh_hold_payment()

        wizard = self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=bill.ids,
        ).create({})
        with self.assertRaises(UserError):
            wizard.action_create_payments()

        bank = self.env['account.journal'].search([
            ('type', '=', 'bank'),
            ('company_id', '=', self.company.id),
        ], limit=1)
        if not bank:
            bank = self._ensure_journal(
                self.env, self.company, 'bank', 'APBK', 'AP Test Bank',
                default_account=self.account_cash,
            )
        Payment = self.env['account.payment']
        payment_vals = {
                'payment_type': 'outbound',
                'partner_type': 'supplier',
                'partner_id': self.partner_a.id,
                'amount': bill.amount_residual,
                'date': fields.Date.today(),
                'journal_id': bank.id,
                'currency_id': bill.currency_id.id,
        }
        if 'invoice_ids' in Payment._fields:
            with self.assertRaises(UserError):
                Payment.create({
                    **payment_vals,
                    'invoice_ids': [Command.set(bill.ids)],
                })
        if 'eh_source_move_ids' in Payment._fields:
            with self.assertRaises(UserError):
                Payment.create({
                    **payment_vals,
                    'eh_source_move_ids': [Command.set(bill.ids)],
                })

        bill_line = bill.line_ids.filtered(
            lambda line: line.account_id.account_type == 'liability_payable',
        )
        clearing = self.post_balanced_move([
            {
                'account': bill_line.account_id,
                'debit': bill.amount_residual,
                'partner': self.partner_a,
            },
            {
                'account': self.account_cash,
                'credit': bill.amount_residual,
                'partner': self.partner_a,
            },
        ])
        clearing_line = clearing.line_ids.filtered(
            lambda line: line.account_id == bill_line.account_id,
        )
        with self.assertRaises(UserError):
            (bill_line + clearing_line).reconcile()

        bill.with_user(self.manager).eh_payment_action_reason = 'Resolved'
        bill.with_user(self.manager).action_eh_release_payment()
        (bill_line + clearing_line).reconcile()
        self.assertTrue(bill_line.reconciled)
        bill.invalidate_recordset(['payment_state', 'eh_release_to_pay'])
        self.assertEqual(bill.eh_release_to_pay, 'settled')

    def test_partner_ap_configuration_is_company_scoped(self):
        company_b = self.env['res.company'].create({
            'name': 'AP Config Isolation B',
            'currency_id': self.company.currency_id.id,
        })
        profile_b = self.env['eh.ap.tolerance.profile'].with_company(
            company_b,
        ).create({
            'name': 'Company B AP Profile',
            'company_id': company_b.id,
            'qty_tolerance_pct': 9.0,
        })
        regex_a = r'(?im)A[:\s]+([A-Z0-9-]+)'
        regex_b = r'(?im)B[:\s]+([A-Z0-9-]+)'
        self.partner_a.with_company(company_b).write({
            'eh_ap_invoice_ref_regex': regex_b,
            'eh_ap_tolerance_profile_id': profile_b.id,
        })
        with self.assertRaises(AccessError):
            self.partner_a.with_user(self.manager).with_company(
                company_b,
            ).write({'eh_ap_invoice_ref_regex': '([A-Z]+)'})
        self.partner_a.with_user(self.manager).with_company(
            self.company,
        ).write({
            'eh_ap_invoice_ref_regex': regex_a,
            'eh_ap_tolerance_profile_id': self.default_profile.id,
        })
        self.assertEqual(
            self.partner_a.with_company(self.company).eh_ap_invoice_ref_regex,
            regex_a,
        )
        self.assertEqual(
            self.partner_a.with_company(company_b).eh_ap_invoice_ref_regex,
            regex_b,
        )
        self.assertEqual(
            self.env['eh.ap.tolerance.profile'].resolve_for_partner(
                self.partner_a, company=company_b,
            ),
            profile_b,
        )

    def test_tolerance_profile_company_is_active_and_immutable(self):
        company_b = self.env['res.company'].create({
            'name': 'AP Profile Isolation B',
            'currency_id': self.company.currency_id.id,
        })
        with self.assertRaises(AccessError):
            self.env['eh.ap.tolerance.profile'].with_user(
                self.manager,
            ).create({
                'name': 'Forged Company B Profile',
                'company_id': company_b.id,
            })
        with self.assertRaises(AccessError):
            self.default_profile.with_user(self.manager).write({
                'company_id': company_b.id,
            })

    def test_regex_policy_rejects_redos_and_unbounded_text(self):
        with self.assertRaises(ValidationError):
            self.partner_a.write({
                'eh_ap_invoice_ref_regex': r'((a+)+)$',
            })
        with self.assertRaises(ValidationError):
            self.company.write({
                'eh_ap_total_regex': r'((a|aa)+)$',
            })
        with self.assertRaises(ValidationError):
            self.partner_a.write({
                'eh_ap_invoice_ref_regex': r'(a+)a(a+)$',
            })
        with self.assertRaises(ValidationError):
            self.partner_a.write({
                'eh_ap_invoice_ref_regex': r'([A-Z]+)$',
            })
        with self.assertRaises(ValidationError):
            self.partner_a.write({
                'eh_ap_invoice_ref_regex': r'(A{1001})',
            })
        with self.assertRaises(UserError):
            self.env['eh.ap.intake']._eh_safe_regex_search(
                r'Invoice[:\s]+([A-Z0-9-]+)',
                'x' * (64 * 1024 + 1),
                'invoice reference',
            )

    def test_source_edit_invalidates_match_and_current_decision(self):
        intake, line = self._make_intake()
        self.assertEqual(intake.state, 'matched')
        self.assertTrue(line.match_evidence_fingerprint)
        line.with_user(self.manager).write({
            'invoice_price': 12.60,
            'subtotal': 126.0,
        })
        intake.invalidate_recordset(['state'])
        line.invalidate_recordset([
            'match_status', 'match_evidence_fingerprint',
        ])
        self.assertEqual(intake.state, 'parsed')
        self.assertEqual(line.match_status, 'pending')
        self.assertFalse(line.match_evidence_fingerprint)

    def test_changed_po_evidence_blocks_post(self):
        intake, _line = self._make_intake(reference='HARD-PO-STALE')
        self.po.order_line.price_unit = 13.0
        with self.assertRaises(UserError):
            intake.with_user(self.manager).action_post()
        self.assertFalse(intake.move_id)

    def test_generated_total_must_exactly_match_captured_total(self):
        intake, _line = self._make_intake(
            reference='HARD-TOTAL', captured_total=126.0,
        )
        with self.assertRaises(UserError):
            with self.env.cr.savepoint():
                intake.with_user(self.manager).action_post()
        intake.invalidate_recordset(['move_id', 'state'])
        self.assertFalse(intake.move_id)
        self.assertEqual(intake.state, 'matched')

    def test_duplicate_override_is_append_only(self):
        first, _line = self._make_intake(reference='HARD-DUP')
        second, _line = self._make_intake(reference='HARD-DUP')
        second.with_user(self.manager).duplicate_override_request_reason = (
            'Verified distinct delivery'
        )
        second.with_user(self.manager).action_override_duplicate()
        log = second.duplicate_override_log_id
        self.assertTrue(log)
        self.assertEqual(log.overridden_by_id, self.manager)
        self.assertEqual(
            log.with_user(self.auditor).read(['intake_id'])[0]['intake_id'][0],
            second.id,
        )
        self.assertEqual(
            second.with_user(self.auditor).read(['name'])[0]['name'],
            second.name,
        )
        with self.assertRaises(AccessError):
            log.write({'reason': 'forged'})
        with self.assertRaises(AccessError):
            log.unlink()
        with self.assertRaises(AccessError):
            self.env['eh.ap.duplicate.override.log'].with_user(
                self.manager,
            ).create({
                'intake_id': first.id,
                'reason': 'forged',
                'evidence_fingerprint': '0' * 64,
                'overridden_by_id': self.manager.id,
                'overridden_at': fields.Datetime.now(),
            })

    def test_line_override_is_append_only(self):
        intake, line = self._make_intake(
            reference='HARD-LINE', qty=11.0,
        )
        self.assertEqual(intake.state, 'exception')
        line.with_user(self.manager).override_reason = 'Approved extra unit'
        line.with_user(self.manager).action_override()
        self.assertEqual(line.match_status, 'overridden')
        self.assertEqual(intake.state, 'matched')
        log = line.decision_log_id
        self.assertEqual(log.decision, 'override')
        self.assertEqual(log.decided_by_id, self.manager)
        with self.assertRaises(AccessError):
            log.write({'reason': 'forged'})
        with self.assertRaises(AccessError):
            log.unlink()

    def test_raw_text_payload_limit(self):
        with self.assertRaises(ValidationError):
            self.env['eh.ap.intake'].create({
                'raw_text': 'x' * (2 * 1024 * 1024 + 1),
            })

    def test_captured_pdf_is_preferred_and_immutable(self):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'raw_text': 'Invoice: HARD-SOURCE\nTotal: 12.50',
        })
        pdf = self.env['ir.attachment'].create({
            'name': 'invoice.pdf',
            'type': 'binary',
            'datas': base64.b64encode(b'%PDF invoice evidence'),
            'mimetype': 'application/pdf',
            'res_model': intake._name,
            'res_id': intake.id,
            'company_id': self.company.id,
        })
        self.env['ir.attachment'].create({
            'name': 'email-logo.png',
            'type': 'binary',
            'datas': base64.b64encode(b'PNG logo'),
            'mimetype': 'image/png',
            'res_model': intake._name,
            'res_id': intake.id,
            'company_id': self.company.id,
        })
        intake.action_parse()
        self.assertEqual(intake.source_attachment_id, pdf)
        self.assertTrue(intake.source_fingerprint)
        with self.assertRaises(UserError):
            pdf.sudo().write({
                'datas': base64.b64encode(b'%PDF replaced'),
            })
        with self.assertRaises(UserError):
            pdf.sudo().unlink()

    def test_public_source_attachment_is_rejected(self):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'raw_text': 'Invoice: HARD-PUBLIC\nTotal: 12.50',
        })
        self.env['ir.attachment'].create({
            'name': 'public-invoice.pdf',
            'type': 'binary',
            'datas': base64.b64encode(b'%PDF public'),
            'mimetype': 'application/pdf',
            'res_model': intake._name,
            'res_id': intake.id,
            'company_id': self.company.id,
            'public': True,
        })
        with self.assertRaises(ValidationError):
            intake.action_parse()

    def test_payment_hold_audit_is_server_owned_and_append_only(self):
        self.product_a.purchase_method = 'purchase'
        po = self._create_purchase_order([(self.product_a, 1, 12.50)])
        po.action_create_invoice()
        bill = po.invoice_ids[:1]
        bill.invoice_date = fields.Date.today()
        bill.action_post()
        with self.assertRaises(AccessError):
            bill.with_user(self.manager).write({'eh_payment_hold': True})
        bill.with_user(self.manager).eh_payment_action_reason = 'Vendor dispute'
        bill.with_user(self.manager).action_eh_hold_payment()
        log = bill.eh_payment_hold_log_ids
        self.assertEqual(len(log), 1)
        self.assertEqual(log.actor_id, self.manager)
        with self.assertRaises(AccessError):
            log.write({'reason': 'forged'})
        with self.assertRaises(AccessError):
            log.unlink()

    def test_wrong_journal_scope_rejected(self):
        sale_journal = self.env['account.journal'].search([
            ('type', '=', 'sale'),
            ('company_id', '=', self.company.id),
        ], limit=1)
        if not sale_journal:
            self.skipTest('No sale journal available.')
        with self.assertRaises(ValidationError):
            self.env['eh.ap.intake'].create({
                'partner_id': self.partner_a.id,
                'journal_id': sale_journal.id,
            })
