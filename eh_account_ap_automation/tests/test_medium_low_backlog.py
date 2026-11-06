# -*- encoding: utf-8 -*-
"""Regressions for the AP Automation medium/low audit closure."""

import hashlib
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.modules.module import get_module_path
from odoo.tests import new_test_user, tagged

from odoo.addons.eh_account_ap_automation.extractors.base import (
    ExtractedLine, LineItemExtractor,
)
from odoo.addons.eh_account_ap_automation.extractors import registry
from odoo.addons.eh_account_ap_automation.models.ap_intake import IrAttachment

from .common import EhApTestCase


@tagged('eh_account_ap_automation', 'integration', 'post_install', '-at_install')
class TestApMediumLowBacklog(EhApTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager',
        )
        cls.manager = new_test_user(
            cls.env,
            login='ap_medium_low_manager',
            groups=(
                'base.group_user,base.group_partner_manager,'
                'eh_account_base.group_eh_manager'
            ),
            company_id=cls.company.id,
        )
        cls.po = cls._create_purchase_order([(cls.product_a, 10, 12.50)])
        cls._confirm_receipt(cls.po)

    def _exception_intake(self, reference):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'purchase_order_id': self.po.id,
            'vendor_reference': reference,
            'bill_date': fields.Date.today(),
            'extracted_total': 137.50,
            'raw_text': 'Invoice: %s\nTotal: 137.50' % reference,
            'state': 'parsed',
        })
        line = self.env['eh.ap.intake.line'].create({
            'intake_id': intake.id,
            'product_id': self.product_a.id,
            'product_code': self.product_a.default_code,
            'invoice_qty': 11.0,
            'invoice_price': 12.50,
            'subtotal': 137.50,
        })
        intake.action_match()
        self.assertEqual(intake.state, 'exception')
        return intake, line

    def _purchase_tax(self, name, amount):
        values = {
            'name': name,
            'amount': amount,
            'amount_type': 'percent',
            'type_tax_use': 'purchase',
            'price_include': False,
            'company_id': self.company.id,
        }
        if 'country_id' in self.env['account.tax']._fields:
            country = (
                getattr(self.company, 'account_fiscal_country_id', False)
                or self.company.country_id
                or self.env.ref('base.us')
            )
            values['country_id'] = country.id
        return self.env['account.tax'].create(values)

    @staticmethod
    def _po_tax_field(po_line):
        return 'tax_ids' if 'tax_ids' in po_line._fields else 'taxes_id'

    def test_default_header_patterns_ignore_date_number_and_subtotal_tokens(self):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'raw_text': (
                'Invoice Date: 2026-08-26\n'
                'Invoice No: SAFE-0042\n'
                'Subtotal: 90.00\n'
                'Total: 99.00\n'
            ),
        })
        intake.action_parse()
        self.assertEqual(intake.vendor_reference, 'SAFE-0042')
        self.assertEqual(intake.extracted_total, 99.0)

    def test_long_email_subject_is_preserved_and_bounded_uniquely(self):
        common = 'Quarterly supplier invoice ' + ('X' * 560)
        first = self.env['eh.ap.intake'].message_new({
            'subject': common + 'A',
            'body': 'Invoice No: MAIL-1\nTotal: 1.00',
            'email_from': 'unknown@example.com',
        })
        second = self.env['eh.ap.intake'].message_new({
            'subject': common + 'B',
            'body': 'Invoice No: MAIL-2\nTotal: 1.00',
            'email_from': 'unknown@example.com',
        })
        self.assertEqual(first.source_email_subject, common + 'A')
        self.assertEqual(len(first.vendor_reference), 512)
        self.assertNotEqual(first.vendor_reference, second.vendor_reference)
        self.assertTrue(first.vendor_reference.endswith(
            hashlib.sha256((common + 'A').encode()).hexdigest()[:16],
        ))
        with self.assertRaises(AccessError):
            self.env['eh.ap.intake'].with_user(self.manager).create({
                'source_email_subject': 'forged outside the mail gateway',
            })

    def test_extractor_key_is_a_registered_selection(self):
        field = self.env['eh.ap.intake']._fields['extractor_key']
        choices = dict(field._description_selection(self.env))
        self.assertIn('claude_llm', choices)
        try:
            self.env['eh.ap.intake'].with_user(self.manager).create({
                'extractor_key': 'misspelled_provider',
            })
        except (ValueError, ValidationError):
            pass
        else:
            self.fail('An unregistered extractor key was accepted.')

    def test_received_edits_do_not_emit_false_invalidation_chatter(self):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
        })
        count = len(intake.message_ids)
        intake.with_user(self.manager).raw_text = (
            'Invoice No: QUIET-1\nTotal: 1.00'
        )
        self.assertEqual(len(intake.message_ids), count)
        intake.action_parse()
        reviewed_count = len(intake.message_ids)
        intake.with_user(self.manager).raw_text = (
            'Invoice No: QUIET-2\nTotal: 2.00'
        )
        self.assertEqual(len(intake.message_ids), reviewed_count + 1)

    def test_match_honours_ordered_service_without_receipt(self):
        service = self.env['product.product'].create({
            'name': 'Manual consulting service',
            'default_code': 'SERVICE-MANUAL',
            'type': 'service',
            'purchase_method': 'purchase',
        })
        po = self._create_purchase_order([(service, 3.0, 100.0)])
        self.assertFalse(po.picking_ids)
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'purchase_order_id': po.id,
            'vendor_reference': 'SERVICE-001',
            'bill_date': fields.Date.today(),
            'extracted_total': 300.0,
            'raw_text': 'Invoice: SERVICE-001\nTotal: 300.00',
            'state': 'parsed',
        })
        line = self.env['eh.ap.intake.line'].create({
            'intake_id': intake.id,
            'product_id': service.id,
            'invoice_qty': 3.0,
            'invoice_price': 100.0,
            'subtotal': 300.0,
        })
        intake.action_match()
        self.assertEqual(line.match_status, 'ok')
        self.assertEqual(line.received_qty, 0.0)

    def test_absolute_tolerance_uses_profile_currency_at_bill_date(self):
        fx = self.env['res.currency'].create({
            'name': 'APTX', 'symbol': 'T', 'rounding': 0.01, 'active': True,
        })
        fx.rate_ids.unlink()
        self.env['res.currency.rate'].create({
            'currency_id': fx.id,
            'name': '2026-08-01',
            'rate': 2.0,
            'company_id': self.company.id,
        })
        profile = self.default_profile
        profile.write({
            'price_tolerance_pct': 0.0,
            'amount_tolerance': 5.0,
        })
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'purchase_order_id': self.po.id,
            'currency_id': fx.id,
            'bill_date': '2026-08-01',
            'state': 'parsed',
        })
        line = self.env['eh.ap.intake.line'].create({
            'intake_id': intake.id,
            'product_id': self.product_a.id,
            'invoice_qty': 10.0,
            'invoice_price': 0.0,
            'subtotal': 0.0,
        })
        po_price = line._eh_po_price_in_bill_currency(self.po.order_line)
        evidence = line._eh_amount_tolerance_evidence(profile)
        self.assertEqual(evidence['source_currency_id'], self.company.currency_id.id)
        self.assertEqual(evidence['bill_currency_id'], fx.id)
        self.assertAlmostEqual(evidence['converted_amount'], 10.0, places=2)
        price = po_price + (evidence['converted_amount'] + 1.0) / 10.0
        line.with_user(self.manager).write({
            'invoice_price': price, 'subtotal': price * 10.0,
        })
        intake.action_match()
        self.assertEqual(line.match_status, 'price_exception')

    def test_automatic_taxes_refresh_but_manual_tax_decision_survives(self):
        tax_a = self._purchase_tax('AP provenance A', 7.1)
        tax_b = self._purchase_tax('AP provenance B', 8.2)
        self.po.order_line.write({
            self._po_tax_field(self.po.order_line): [Command.set(tax_a.ids)],
        })
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'purchase_order_id': self.po.id,
            'vendor_reference': 'TAX-PROVENANCE',
            'raw_text': 'Invoice: TAX-PROVENANCE\nTotal: 125.00',
            'state': 'parsed',
        })
        line = self.env['eh.ap.intake.line'].create({
            'intake_id': intake.id,
            'product_id': self.product_a.id,
            'invoice_qty': 10.0,
            'invoice_price': 12.50,
            'subtotal': 125.0,
        })
        intake.action_match()
        self.assertEqual(line.tax_ids, tax_a)
        self.assertEqual(line.tax_provenance, 'po')
        intake.with_user(self.manager).raw_text = (
            'Invoice: TAX-PROVENANCE\nTotal: 126.00'
        )
        self.assertFalse(line.tax_ids)
        self.po.order_line.write({
            self._po_tax_field(self.po.order_line): [Command.set(tax_b.ids)],
        })
        intake.action_match()
        self.assertEqual(line.tax_ids, tax_b)
        line.with_user(self.manager).tax_ids = [Command.set(tax_a.ids)]
        self.assertEqual(line.tax_provenance, 'manual')
        self.po.order_line.write({
            self._po_tax_field(self.po.order_line): [Command.set(tax_b.ids)],
        })
        intake.action_match()
        self.assertEqual(line.tax_ids, tax_a)
        self.assertEqual(line.tax_provenance, 'manual')

    def test_extractor_tax_rate_maps_to_exact_purchase_tax(self):
        tax = self._purchase_tax('AP extractor exact 7.777', 7.777)

        class TaxExtractor(LineItemExtractor):
            EXTRACTOR_KEY = 'test_ap_tax_7777'
            EXTRACTOR_LABEL = 'Test AP exact tax'

            def extract(self, document_bytes, mime_type, hints=None):
                return [ExtractedLine(
                    description='Extracted consulting', quantity=2.0,
                    unit_price=50.0, line_total=100.0,
                    tax_rate_pct=7.777, confidence=1.0,
                )]

        registry.register_extractor(TaxExtractor)
        try:
            intake = self.env['eh.ap.intake'].create({
                'partner_id': self.partner_a.id,
                'raw_text': 'Invoice No: TAX-7777\nTotal: 107.78',
                'extractor_key': TaxExtractor.EXTRACTOR_KEY,
            })
            intake.action_parse()
            line = intake.line_ids
            self.assertEqual(line.tax_ids, tax)
            self.assertEqual(line.tax_provenance, 'extractor')
            self.assertEqual(
                line._build_invoice_line_vals()['tax_ids'],
                [(6, 0, tax.ids)],
            )
            total = tax.compute_all(
                50.0, currency=self.company.currency_id, quantity=2.0,
            )['total_included']
            self.assertAlmostEqual(total, 107.78, places=2)
        finally:
            registry._EXTRACTORS.pop(TaxExtractor.EXTRACTOR_KEY, None)

    def test_rejected_line_can_be_reopened_with_append_only_audit(self):
        intake, line = self._exception_intake('REOPEN-001')
        line.override_reason = 'Wrong delivery schedule'
        line.action_reject_line()
        self.assertEqual(line.match_status, 'rejected')
        line.override_reason = 'Correct schedule now supplied'
        line.action_reopen_line()
        self.assertEqual(intake.state, 'parsed')
        self.assertEqual(line.match_status, 'pending')
        self.assertEqual(
            set(line.decision_log_ids.mapped('decision')),
            {'reject', 'reopen'},
        )

    def test_intake_with_decision_history_has_explicit_retention_error(self):
        intake, line = self._exception_intake('RETAIN-001')
        line.override_reason = 'Reject with durable evidence'
        line.action_reject_line()
        with self.assertRaisesRegex(UserError, 'append-only decision history'):
            intake.unlink()

    def test_unrelated_attachment_skips_ap_lock_and_source_search(self):
        attachment = self.env['ir.attachment'].create({
            'name': 'ordinary-note.txt',
            'type': 'binary',
            'raw': b'ordinary',
            'res_model': 'res.partner',
            'res_id': self.partner_a.id,
        })
        with patch.object(
                IrAttachment, '_eh_lock_ap_source_mutation',
                side_effect=AssertionError('unrelated AP advisory lock')):
            attachment.write({'name': 'ordinary-note-renamed.txt'})

    def test_views_disable_drag_and_expose_only_valid_line_decisions(self):
        root = Path(get_module_path('eh_account_ap_automation'))
        xml = ElementTree.parse(root / 'views/ap_intake_views.xml').getroot()
        kanban = xml.find('.//kanban')
        self.assertEqual(kanban.attrib.get('records_draggable'), 'false')
        reject = xml.find(".//button[@name='action_reject_line']")
        modifier = (
            reject.attrib.get('invisible', '')
            + reject.attrib.get('attrs', '')
        )
        self.assertIn('match_status', modifier)
        self.assertIn('qty_exception', modifier)
        self.assertIsNotNone(xml.find(".//button[@name='action_reopen_line']"))


@tagged('eh_account_ap_automation', 'performance', 'post_install', '-at_install')
class TestApBulkParseRegression(EhApTestCase):

    def test_hundred_lines_use_bulk_product_resolution_and_create(self):
        raw = ['Invoice No: BULK-100']
        raw.extend('WIDGET-001 1 12.50' for _index in range(100))
        raw.append('Total: 1250.00')
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'raw_text': '\n'.join(raw),
        })
        with self.assertQueryCount(__system__=450):
            intake.action_parse()
        self.assertEqual(len(intake.line_ids), 100)
