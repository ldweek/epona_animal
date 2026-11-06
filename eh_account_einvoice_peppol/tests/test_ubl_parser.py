# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
Pure-Python tests for the Peppol BIS Billing 3.0 (UBL 2.1) parser.

The parser is the inverse of the generator. We round-trip a generated
XML payload through the parser and assert the parsed dict reproduces
the original supplier / customer / line shape (within UBL's lossy
serialisation: numerics become Decimal-from-text and addresses lose
optional sub-fields the generator omits).
"""

import datetime
from unittest import mock

from lxml import etree

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_einvoice_peppol.tools.ubl_generator import (
    make_invoice_payload, render_invoice_xml,
)
from odoo.addons.eh_account_einvoice_peppol.tools.ubl_parser import (
    PeppolParserError, parse_invoice_xml,
)
from odoo.addons.eh_account_einvoice_peppol.tools import ubl_parser


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
        {
            'id': '2', 'description': 'License fee',
            'quantity': '1', 'unit_code': 'EA',
            'unit_price': '500.00', 'line_total': '500.00',
            'tax_category_code': 'S', 'tax_rate_pct': '10.00',
        },
    ]


def _categories():
    return [
        {
            'category_code': 'S', 'rate_pct': '10.00',
            'taxable_amount': '1500.00', 'tax_amount': '150.00',
        },
    ]


def _make_xml(document_type='invoice'):
    payload = make_invoice_payload(
        invoice_number='INV-2026-00042',
        issue_date=datetime.date(2026, 5, 1),
        due_date=datetime.date(2026, 5, 31),
        currency_code='AUD',
        supplier=_supplier(), customer=_customer(),
        lines=_lines(), tax_categories=_categories(),
        document_type=document_type,
        order_reference='PO-2026-100',
        note='Net 30',
    )
    return render_invoice_xml(payload)


@tagged('post_install', '-at_install')
class PeppolParserHappyPathTest(TransactionCase):

    def test_invoice_root_yields_invoice_document_type(self):
        result = parse_invoice_xml(_make_xml('invoice'))
        self.assertEqual(result['document_type'], 'invoice')

    def test_credit_note_root_yields_credit_note_document_type(self):
        result = parse_invoice_xml(_make_xml('credit_note'))
        self.assertEqual(result['document_type'], 'credit_note')

    def test_header_round_trip(self):
        result = parse_invoice_xml(_make_xml())
        self.assertEqual(result['invoice_number'], 'INV-2026-00042')
        self.assertEqual(
            result['issue_date'], datetime.date(2026, 5, 1),
        )
        self.assertEqual(
            result['due_date'], datetime.date(2026, 5, 31),
        )
        self.assertEqual(result['currency_code'], 'AUD')
        self.assertEqual(result['note'], 'Net 30')
        self.assertEqual(result['order_reference'], 'PO-2026-100')

    def test_supplier_and_customer_round_trip(self):
        result = parse_invoice_xml(_make_xml())
        self.assertEqual(
            result['supplier']['name'], 'Heritage Books Pty Ltd',
        )
        self.assertEqual(
            result['supplier']['endpoint_id'], '83914571673',
        )
        self.assertEqual(
            result['supplier']['endpoint_scheme'], '0151',
        )
        self.assertEqual(
            result['customer']['name'], 'Buyer Co',
        )
        self.assertEqual(
            result['customer']['address']['city'], 'Sydney',
        )

    def test_lines_round_trip(self):
        result = parse_invoice_xml(_make_xml())
        self.assertEqual(len(result['lines']), 2)
        first = result['lines'][0]
        self.assertEqual(first['id'], '1')
        self.assertEqual(first['description'], 'Consulting')
        self.assertEqual(first['unit_code'], 'HUR')
        self.assertAlmostEqual(first['quantity'], 10.0, places=2)
        self.assertAlmostEqual(first['unit_price'], 100.0, places=2)
        self.assertAlmostEqual(first['line_total'], 1000.0, places=2)
        self.assertEqual(first['tax_category_code'], 'S')
        self.assertAlmostEqual(first['tax_rate_pct'], 10.0, places=2)

    def test_tax_categories_round_trip(self):
        result = parse_invoice_xml(_make_xml())
        self.assertEqual(len(result['tax_categories']), 1)
        cat = result['tax_categories'][0]
        self.assertEqual(cat['category_code'], 'S')
        self.assertAlmostEqual(cat['rate_pct'], 10.0, places=2)
        self.assertAlmostEqual(cat['taxable_amount'], 1500.0, places=2)
        self.assertAlmostEqual(cat['tax_amount'], 150.0, places=2)

    def test_legal_monetary_totals_round_trip(self):
        result = parse_invoice_xml(_make_xml())
        self.assertEqual(result['tax_amount'], 150.0)
        self.assertEqual(result['monetary_totals'], {
            'line_extension_amount': 1500.0,
            'tax_exclusive_amount': 1500.0,
            'tax_inclusive_amount': 1650.0,
            'payable_amount': 1650.0,
        })

    def test_valid_line_allowance_books_declared_net(self):
        xml = _make_xml()
        xml = xml.replace(
            b'<cbc:PriceAmount currencyID="AUD">100.00</cbc:PriceAmount>',
            b'<cbc:PriceAmount currencyID="AUD">110.00</cbc:PriceAmount>',
            1,
        )
        marker = b'<cac:Item>'
        allowance = (
            b'<cac:AllowanceCharge>'
            b'<cbc:ChargeIndicator>false</cbc:ChargeIndicator>'
            b'<cbc:Amount currencyID="AUD">100.00</cbc:Amount>'
            b'</cac:AllowanceCharge>'
        )
        xml = xml.replace(marker, allowance + marker, 1)

        result = parse_invoice_xml(xml)

        self.assertEqual(result['lines'][0]['unit_price'], 110.0)
        self.assertEqual(result['lines'][0]['line_total'], 1000.0)

    def test_r120_line_net_rounding_slack_is_accepted(self):
        xml = _make_xml().replace(
            b'<cbc:PriceAmount currencyID="AUD">100.00</cbc:PriceAmount>',
            b'<cbc:PriceAmount currencyID="AUD">100.0016</cbc:PriceAmount>',
            1,
        )

        result = parse_invoice_xml(xml)

        self.assertEqual(result['lines'][0]['line_total'], 1000.0)

    def test_vat_breakdown_rounding_slack_is_accepted(self):
        xml = _make_xml().replace(
            b'<cbc:TaxableAmount currencyID="AUD">1500.00</cbc:TaxableAmount>',
            b'<cbc:TaxableAmount currencyID="AUD">1500.50</cbc:TaxableAmount>',
            1,
        ).replace(
            b'<cbc:TaxAmount currencyID="AUD">150.00</cbc:TaxAmount>',
            b'<cbc:TaxAmount currencyID="AUD">150.05</cbc:TaxAmount>',
        ).replace(
            b'<cbc:TaxInclusiveAmount currencyID="AUD">1650.00</cbc:TaxInclusiveAmount>',
            b'<cbc:TaxInclusiveAmount currencyID="AUD">1650.05</cbc:TaxInclusiveAmount>',
        ).replace(
            b'<cbc:PayableAmount currencyID="AUD">1650.00</cbc:PayableAmount>',
            b'<cbc:PayableAmount currencyID="AUD">1650.05</cbc:PayableAmount>',
        )

        result = parse_invoice_xml(xml)

        self.assertEqual(result['tax_categories'][0]['taxable_amount'], 1500.5)
        self.assertEqual(result['tax_amount'], 150.05)


@tagged('post_install', '-at_install')
class PeppolParserErrorTest(TransactionCase):

    def test_doctype_internal_entity_is_rejected(self):
        xml = (
            b'<!DOCTYPE Invoice [<!ENTITY probe "expanded">]>'
            b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:'
            b'Invoice-2">&probe;</Invoice>'
        )
        with self.assertRaisesRegex(PeppolParserError, 'DOCTYPE|parse'):
            parse_invoice_xml(xml)

    def test_doctype_external_entity_is_rejected(self):
        xml = (
            b'<!DOCTYPE Invoice [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:'
            b'Invoice-2">&xxe;</Invoice>'
        )
        with self.assertRaisesRegex(PeppolParserError, 'DOCTYPE|parse'):
            parse_invoice_xml(xml)

    def test_doctype_parameter_entity_is_rejected(self):
        xml = (
            b'<!DOCTYPE Invoice [<!ENTITY % remote SYSTEM '
            b'"http://127.0.0.1/evil.dtd">%remote;]>'
            b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:'
            b'Invoice-2"/>'
        )
        with self.assertRaisesRegex(PeppolParserError, 'DOCTYPE|parse'):
            parse_invoice_xml(xml)

    def test_doctype_entity_expansion_is_rejected(self):
        xml = (
            b'<!DOCTYPE Invoice ['
            b'<!ENTITY a "1234567890">'
            b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
            b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>'
            b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:'
            b'Invoice-2">&c;</Invoice>'
        )
        with self.assertRaisesRegex(PeppolParserError, 'DOCTYPE|parse'):
            parse_invoice_xml(xml)

    def test_caller_parsed_doctype_tree_is_rejected(self):
        xml = (
            b'<!DOCTYPE Invoice [<!ENTITY probe "expanded">]>'
            b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:'
            b'Invoice-2">&probe;</Invoice>'
        )
        root = etree.fromstring(
            xml,
            etree.XMLParser(resolve_entities=False, load_dtd=True),
        )
        with self.assertRaisesRegex(PeppolParserError, 'DOCTYPE'):
            parse_invoice_xml(root)

    def test_oversized_xml_rejected_before_parse(self):
        with mock.patch.object(ubl_parser, 'MAX_UBL_BYTES', 16):
            with self.assertRaisesRegex(PeppolParserError, 'maximum allowed'):
                parse_invoice_xml(b' ' * 17)

    def test_unparseable_xml_raises(self):
        with self.assertRaises(PeppolParserError):
            parse_invoice_xml(b"<not valid xml")

    def test_wrong_root_raises(self):
        with self.assertRaises(PeppolParserError):
            parse_invoice_xml(
                b'<?xml version="1.0"?><Foo xmlns="bar"/>',
            )

    def test_missing_invoice_number_raises(self):
        # Strip the cbc:ID before the parse.
        xml = _make_xml().decode('utf-8')
        # Remove the FIRST cbc:ID (the document-level one); leaves
        # the per-line ones intact.
        # Actually the generator may use the prefixed cbc element from
        # the document namespace declaration, not a per-element xmlns.
        # Strip the first cbc:ID via a simpler regex on the prefixed
        # form.
        import re
        xml_no_id = re.sub(r'<cbc:ID>.*?</cbc:ID>', '', xml, count=1)
        with self.assertRaises(PeppolParserError):
            parse_invoice_xml(xml_no_id)

    def test_manipulated_line_total_rejected(self):
        xml = _make_xml().replace(
            b'<cbc:LineExtensionAmount currencyID="AUD">1000.00</cbc:LineExtensionAmount>',
            b'<cbc:LineExtensionAmount currencyID="AUD">999.00</cbc:LineExtensionAmount>',
            1,
        )
        with self.assertRaisesRegex(
                PeppolParserError, 'LineExtensionAmount.*inconsistent'):
            parse_invoice_xml(xml)

    def test_mismatched_amount_currency_rejected(self):
        xml = _make_xml().replace(
            b'<cbc:TaxAmount currencyID="AUD">150.00</cbc:TaxAmount>',
            b'<cbc:TaxAmount currencyID="USD">150.00</cbc:TaxAmount>',
            1,
        )
        with self.assertRaisesRegex(PeppolParserError, 'currencyID'):
            parse_invoice_xml(xml)

    def test_line_tax_category_must_match_subtotal(self):
        xml = _make_xml().replace(
            b'<cbc:Percent>10.00</cbc:Percent>',
            b'<cbc:Percent>9.00</cbc:Percent>',
            1,
        )
        with self.assertRaisesRegex(
                PeppolParserError, 'categories.*disagree'):
            parse_invoice_xml(xml)

    def test_document_allowance_rejected(self):
        xml = _make_xml().replace(
            b'<cac:TaxTotal>',
            b'<cac:AllowanceCharge>'
            b'<cbc:ChargeIndicator>false</cbc:ChargeIndicator>'
            b'<cbc:Amount currencyID="AUD">1.00</cbc:Amount>'
            b'</cac:AllowanceCharge><cac:TaxTotal>',
            1,
        )
        with self.assertRaisesRegex(
                PeppolParserError, 'Unsupported document-level'):
            parse_invoice_xml(xml)

    def test_payable_total_inconsistency_rejected(self):
        xml = _make_xml().replace(
            b'<cbc:PayableAmount currencyID="AUD">1650.00</cbc:PayableAmount>',
            b'<cbc:PayableAmount currencyID="AUD">1649.00</cbc:PayableAmount>',
            1,
        )
        with self.assertRaisesRegex(PeppolParserError, 'PayableAmount'):
            parse_invoice_xml(xml)

    def test_r120_line_net_outside_slack_is_rejected(self):
        xml = _make_xml().replace(
            b'<cbc:PriceAmount currencyID="AUD">100.00</cbc:PriceAmount>',
            b'<cbc:PriceAmount currencyID="AUD">100.003</cbc:PriceAmount>',
            1,
        )
        with self.assertRaisesRegex(PeppolParserError, 'LineExtensionAmount'):
            parse_invoice_xml(xml)

    def test_vat_breakdown_one_unit_boundary_is_rejected(self):
        xml = _make_xml().replace(
            b'<cbc:TaxableAmount currencyID="AUD">1500.00</cbc:TaxableAmount>',
            b'<cbc:TaxableAmount currencyID="AUD">1501.00</cbc:TaxableAmount>',
            1,
        ).replace(
            b'<cbc:TaxAmount currencyID="AUD">150.00</cbc:TaxAmount>',
            b'<cbc:TaxAmount currencyID="AUD">150.10</cbc:TaxAmount>',
        ).replace(
            b'<cbc:TaxInclusiveAmount currencyID="AUD">1650.00</cbc:TaxInclusiveAmount>',
            b'<cbc:TaxInclusiveAmount currencyID="AUD">1650.10</cbc:TaxInclusiveAmount>',
        ).replace(
            b'<cbc:PayableAmount currencyID="AUD">1650.00</cbc:PayableAmount>',
            b'<cbc:PayableAmount currencyID="AUD">1650.10</cbc:PayableAmount>',
        )
        with self.assertRaisesRegex(PeppolParserError, 'TaxableAmount'):
            parse_invoice_xml(xml)
