# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
Pure-Python tests for the Peppol BIS Billing 3.0 (UBL 2.1) generator.

Runs without Odoo so the test envelope is fast.
"""

import datetime
import unittest

from odoo.tests import TransactionCase, tagged

from lxml import etree

from odoo.addons.eh_account_einvoice_peppol.tools.ubl_generator import (
    PeppolGeneratorError,
    make_invoice_payload,
    render_invoice_xml,
    validate_rendered,
)


_NS_INVOICE = 'urn:oasis:names:specification:ubl:schema:xsd:Invoice-2'
_NS_CREDIT = 'urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2'
_NS_CBC = 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'
_NS_CAC = 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2'


def _supplier():
    return {
        'name': 'Heritage Books Pty Ltd',
        'endpoint_id': '83914571673',
        'endpoint_scheme': '0151',
        'country_code': 'AU',
        'vat_id': '83914571673',
        'address': {
            'street': '1 Heritage Lane', 'city': 'Melbourne',
            'postcode': '3000', 'country': 'AU',
        },
    }


def _customer():
    return {
        'name': 'Buyer Co',
        'endpoint_id': '53004085616',
        'endpoint_scheme': '0151',
        'country_code': 'AU',
        'vat_id': '53004085616',
        'address': {
            'street': '2 Buyer Street', 'city': 'Sydney',
            'postcode': '2000', 'country': 'AU',
        },
    }


def _lines():
    return [
        {
            'id': '1', 'description': 'Consulting',
            'quantity': '10', 'unit_code': 'HUR',
            'unit_price': '100.00', 'line_total': '1000.00',
            'tax_category_code': 'S', 'tax_rate_pct': '10.00',
        },
    ]


def _categories():
    return [
        {
            'category_code': 'S', 'rate_pct': '10.00',
            'taxable_amount': '1000.00', 'tax_amount': '100.00',
        },
    ]


@tagged('post_install', '-at_install')
class PeppolPayloadValidationTest(TransactionCase):
    """Required fields surface explicit errors, never silent defaults."""

    def test_missing_invoice_number_rejected(self):
        with self.assertRaises(PeppolGeneratorError):
            make_invoice_payload(
                invoice_number=None,
                issue_date=datetime.date(2026, 5, 1),
                due_date=datetime.date(2026, 5, 31),
                currency_code='AUD',
                supplier=_supplier(), customer=_customer(),
                lines=_lines(), tax_categories=_categories(),
            )

    def test_missing_currency_rejected(self):
        with self.assertRaises(PeppolGeneratorError):
            make_invoice_payload(
                invoice_number='INV/0001',
                issue_date=datetime.date(2026, 5, 1),
                due_date=datetime.date(2026, 5, 31),
                currency_code=None,
                supplier=_supplier(), customer=_customer(),
                lines=_lines(), tax_categories=_categories(),
            )

    def test_issue_date_must_be_date_instance(self):
        with self.assertRaises(PeppolGeneratorError):
            make_invoice_payload(
                invoice_number='INV/0001',
                issue_date='2026-05-01',  # str, not date
                due_date=datetime.date(2026, 5, 31),
                currency_code='AUD',
                supplier=_supplier(), customer=_customer(),
                lines=_lines(), tax_categories=_categories(),
            )

    def test_unknown_document_type_rejected(self):
        with self.assertRaises(PeppolGeneratorError):
            make_invoice_payload(
                invoice_number='INV/0001',
                issue_date=datetime.date(2026, 5, 1),
                due_date=datetime.date(2026, 5, 31),
                currency_code='AUD',
                supplier=_supplier(), customer=_customer(),
                lines=_lines(), tax_categories=_categories(),
                document_type='proforma',
            )

    def test_missing_due_date_is_omitted_not_fabricated(self):
        payload = make_invoice_payload(
            invoice_number='INV/0001',
            issue_date=datetime.date(2026, 5, 1),
            due_date=None,
            currency_code='AUD',
            supplier=_supplier(), customer=_customer(),
            lines=_lines(), tax_categories=_categories(),
        )

        root = etree.fromstring(render_invoice_xml(payload))

        self.assertIsNone(root.find('{%s}DueDate' % _NS_CBC))


@tagged('post_install', '-at_install')
class PeppolRenderRoundTripTest(TransactionCase):
    """Render and parse back; assert namespace, structure, and totals."""

    def setUp(self):
        super().setUp()
        self.payload = make_invoice_payload(
            invoice_number='INV/2026/0001',
            issue_date=datetime.date(2026, 5, 1),
            due_date=datetime.date(2026, 5, 31),
            currency_code='AUD',
            supplier=_supplier(), customer=_customer(),
            lines=_lines(), tax_categories=_categories(),
            buyer_reference='REF-9001',
        )

    def test_xml_is_well_formed(self):
        xml = render_invoice_xml(self.payload)
        # parses back without raising
        etree.fromstring(xml)

    def test_root_namespace_is_invoice(self):
        xml = render_invoice_xml(self.payload)
        root = etree.fromstring(xml)
        self.assertEqual(root.tag, '{%s}Invoice' % _NS_INVOICE)

    def test_customisation_id_present(self):
        xml = render_invoice_xml(self.payload)
        root = etree.fromstring(xml)
        cust = root.find('{%s}CustomizationID' % _NS_CBC)
        self.assertIsNotNone(cust)
        self.assertIn('en16931', cust.text)
        self.assertIn('peppol', cust.text)

    def test_invoice_id_matches_payload(self):
        xml = render_invoice_xml(self.payload)
        root = etree.fromstring(xml)
        idnode = root.find('{%s}ID' % _NS_CBC)
        self.assertEqual(idnode.text, 'INV/2026/0001')

    def test_currency_code_stamped(self):
        xml = render_invoice_xml(self.payload)
        root = etree.fromstring(xml)
        cur = root.find('{%s}DocumentCurrencyCode' % _NS_CBC)
        self.assertEqual(cur.text, 'AUD')

    def test_supplier_party_present(self):
        xml = render_invoice_xml(self.payload)
        root = etree.fromstring(xml)
        sup = root.find('{%s}AccountingSupplierParty' % _NS_CAC)
        self.assertIsNotNone(sup)

    def test_customer_party_present(self):
        xml = render_invoice_xml(self.payload)
        root = etree.fromstring(xml)
        cust = root.find('{%s}AccountingCustomerParty' % _NS_CAC)
        self.assertIsNotNone(cust)

    def test_invoice_line_count(self):
        xml = render_invoice_xml(self.payload)
        root = etree.fromstring(xml)
        lines = root.findall('{%s}InvoiceLine' % _NS_CAC)
        self.assertEqual(len(lines), 1)

    def test_tax_currency_and_reviewed_monetary_totals_render(self):
        payload = make_invoice_payload(
            invoice_number='INV/2026/0002',
            issue_date=datetime.date(2026, 5, 1),
            due_date=datetime.date(2026, 5, 31),
            currency_code='EUR',
            tax_currency_code='AUD',
            supplier=_supplier(), customer=_customer(),
            lines=_lines(), tax_categories=_categories(),
            monetary_totals={
                'line_extension': 1000.0,
                'tax_exclusive': 1000.0,
                'tax_inclusive': 1100.0,
                'prepaid': 100.0,
                'rounding': 0.05,
                'payable': 1000.05,
                'tax_currency_amount': 160.0,
            },
        )

        xml = render_invoice_xml(payload)
        root = etree.fromstring(xml)
        monetary = root.find('{%s}LegalMonetaryTotal' % _NS_CAC)

        self.assertEqual(
            root.find('{%s}TaxCurrencyCode' % _NS_CBC).text, 'AUD',
        )
        tax_totals = root.findall('{%s}TaxTotal' % _NS_CAC)
        self.assertEqual(len(tax_totals), 2)
        self.assertEqual(
            tax_totals[1].find('{%s}TaxAmount' % _NS_CBC).get('currencyID'),
            'AUD',
        )
        self.assertEqual(
            monetary.find('{%s}PrepaidAmount' % _NS_CBC).text, '100.00',
        )
        self.assertEqual(
            monetary.find('{%s}PayableRoundingAmount' % _NS_CBC).text,
            '0.05',
        )
        self.assertEqual(
            monetary.find('{%s}PayableAmount' % _NS_CBC).text, '1000.05',
        )
        self.assertTrue(validate_rendered(xml))


@tagged('post_install', '-at_install')
class PeppolCreditNoteTest(TransactionCase):

    def test_credit_note_uses_credit_namespace(self):
        payload = make_invoice_payload(
            invoice_number='CN/2026/0001',
            issue_date=datetime.date(2026, 5, 1),
            due_date=datetime.date(2026, 5, 31),
            currency_code='AUD',
            supplier=_supplier(), customer=_customer(),
            lines=_lines(), tax_categories=_categories(),
            document_type='credit_note',
        )
        xml = render_invoice_xml(payload)
        root = etree.fromstring(xml)
        self.assertEqual(root.tag, '{%s}CreditNote' % _NS_CREDIT)

    def test_credit_note_type_code_default(self):
        payload = make_invoice_payload(
            invoice_number='CN/2026/0001',
            issue_date=datetime.date(2026, 5, 1),
            due_date=datetime.date(2026, 5, 31),
            currency_code='AUD',
            supplier=_supplier(), customer=_customer(),
            lines=_lines(), tax_categories=_categories(),
            document_type='credit_note',
        )
        self.assertEqual(payload['invoice_type_code'], '381')

    def test_linked_credit_note_renders_billing_reference(self):
        payload = make_invoice_payload(
            invoice_number='CN/2026/0001',
            issue_date=datetime.date(2026, 5, 1),
            due_date=datetime.date(2026, 5, 31),
            currency_code='AUD',
            supplier=_supplier(), customer=_customer(),
            lines=_lines(), tax_categories=_categories(),
            document_type='credit_note',
            billing_reference='INV/2026/0001',
        )
        root = etree.fromstring(render_invoice_xml(payload))
        reference = root.find(
            '{%s}BillingReference/{%s}InvoiceDocumentReference/{%s}ID'
            % (_NS_CAC, _NS_CAC, _NS_CBC)
        )
        self.assertIsNotNone(reference)
        self.assertEqual(reference.text, 'INV/2026/0001')


@tagged('post_install', '-at_install')
class PeppolDiscountedLineTest(TransactionCase):
    """BR-CO-10: on a discounted line the item net price must tie to the
    line net amount (PriceAmount x Quantity == LineExtensionAmount).
    """

    def _discounted_payload(self):
        # 10 units, list price 100.00, but line net is 900.00 (a 10%
        # discount). Gross 10 x 100 = 1000.00 != 900.00, so the net price
        # is 90.0000 and a line AllowanceCharge carries the 10.0000/unit.
        lines = [{
            'id': '1', 'description': 'Consulting',
            'quantity': '10', 'unit_code': 'HUR',
            'unit_price': '90.00', 'gross_unit_price': '100.00',
            'line_total': '900.00',
            'tax_category_code': 'S', 'tax_rate_pct': '10.00',
        }]
        categories = [{
            'category_code': 'S', 'rate_pct': '10.00',
            'taxable_amount': '900.00', 'tax_amount': '90.00',
        }]
        return make_invoice_payload(
            invoice_number='INV/2026/0009',
            issue_date=datetime.date(2026, 5, 1),
            due_date=datetime.date(2026, 5, 31),
            currency_code='AUD',
            supplier=_supplier(), customer=_customer(),
            lines=lines, tax_categories=categories,
        )

    def test_price_times_qty_ties_to_line_extension(self):
        xml = render_invoice_xml(self._discounted_payload())
        root = etree.fromstring(xml)
        line = root.find('{%s}InvoiceLine' % _NS_CAC)
        lex = line.find('{%s}LineExtensionAmount' % _NS_CBC)
        qty = line.find('{%s}InvoicedQuantity' % _NS_CBC)
        price = line.find(
            '{%s}Price/{%s}PriceAmount' % (_NS_CAC, _NS_CBC))
        from decimal import Decimal
        self.assertEqual(Decimal(lex.text), Decimal('900.00'))
        # net price x quantity ties exactly to the line extension amount
        self.assertEqual(
            (Decimal(price.text) * Decimal(qty.text)).quantize(
                Decimal('0.01')),
            Decimal(lex.text),
        )

    def test_line_allowance_charge_carries_discount(self):
        xml = render_invoice_xml(self._discounted_payload())
        root = etree.fromstring(xml)
        charge = root.find(
            '{%s}InvoiceLine/{%s}Price/{%s}AllowanceCharge'
            % (_NS_CAC, _NS_CAC, _NS_CAC))
        self.assertIsNotNone(charge)
        indicator = charge.find('{%s}ChargeIndicator' % _NS_CBC)
        self.assertEqual(indicator.text, 'false')
        base = charge.find('{%s}BaseAmount' % _NS_CBC)
        from decimal import Decimal
        self.assertEqual(Decimal(base.text), Decimal('100.0000'))

    def test_validate_rendered_passes_on_discounted_line(self):
        xml = render_invoice_xml(self._discounted_payload())
        self.assertTrue(validate_rendered(xml))

    def test_validate_rendered_rejects_gross_price_mismatch(self):
        # Hand-build the pre-fix defect: gross PriceAmount 100.00 against a
        # discounted LineExtensionAmount 900.00 with no AllowanceCharge.
        # 100.00 x 10 = 1000.00 != 900.00, so validate_rendered must catch
        # the BR-CO-10 violation.
        bad = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Invoice xmlns="%(inv)s" xmlns:cbc="%(cbc)s" '
            'xmlns:cac="%(cac)s">'
            '<cbc:CustomizationID>x</cbc:CustomizationID>'
            '<cbc:ProfileID>x</cbc:ProfileID>'
            '<cbc:ID>INV/2026/0009</cbc:ID>'
            '<cbc:IssueDate>2026-05-01</cbc:IssueDate>'
            '<cbc:DocumentCurrencyCode>AUD</cbc:DocumentCurrencyCode>'
            '<cac:AccountingSupplierParty/>'
            '<cac:AccountingCustomerParty/>'
            '<cac:LegalMonetaryTotal>'
            '<cbc:LineExtensionAmount currencyID="AUD">900.00'
            '</cbc:LineExtensionAmount>'
            '</cac:LegalMonetaryTotal>'
            '<cac:InvoiceLine>'
            '<cbc:ID>1</cbc:ID>'
            '<cbc:InvoicedQuantity unitCode="HUR">10.0000'
            '</cbc:InvoicedQuantity>'
            '<cbc:LineExtensionAmount currencyID="AUD">900.00'
            '</cbc:LineExtensionAmount>'
            '<cac:Price>'
            '<cbc:PriceAmount currencyID="AUD">100.00</cbc:PriceAmount>'
            '</cac:Price>'
            '</cac:InvoiceLine>'
            '</Invoice>'
        ) % {'inv': _NS_INVOICE, 'cbc': _NS_CBC, 'cac': _NS_CAC}
        with self.assertRaises(PeppolGeneratorError):
            validate_rendered(bad.encode('utf-8'))


if __name__ == '__main__':
    unittest.main()
