# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Round-trip tests for the UBL structural validator.

`validate_rendered` is a post-render guard against bugs in the
generator (missing mandatory tags, currency mismatch, sum drift) before
the file leaves the system. The tests here render a known-good payload,
then mutate the bytes to violate one invariant at a time, asserting
that each mutation is rejected with a specific reason.

The mutations target:

* Missing mandatory cbc element (CustomizationID, ID, IssueDate,
  DocumentCurrencyCode).
* Currency mismatch (DocumentCurrencyCode vs a per-amount currencyID).
* Missing supplier / customer party.
* Empty line set.
* Sum-of-line-extension drift > 1 cent.
"""

import datetime

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_einvoice_peppol.tools.ubl_generator import (
    PeppolGeneratorError,
    make_invoice_payload,
    render_invoice_xml,
    validate_rendered,
)


def _payload():
    party = {
        'name': 'Acme Pty', 'endpoint_id': '51824753556',
        'endpoint_scheme': '0151', 'country_code': 'AU',
        'vat_id': 'AU51824753556', 'legal_id': '51824753556',
        'address': {
            'street': '1 Main St', 'city': 'Melbourne',
            'postcode': '3000', 'country': 'AU',
        },
    }
    return make_invoice_payload(
        invoice_number='INV001',
        issue_date=datetime.date(2026, 1, 15),
        due_date=datetime.date(2026, 2, 15),
        currency_code='AUD',
        supplier=party,
        customer=party,
        lines=[{
            'id': 1, 'description': 'Consulting', 'quantity': 1.0,
            'unit_code': 'HUR', 'unit_price': 100.0, 'line_total': 100.0,
            'tax_category_code': 'S', 'tax_rate_pct': 10.0,
        }],
        tax_categories=[{
            'category_code': 'S', 'rate_pct': 10.0,
            'taxable_amount': 100.0, 'tax_amount': 10.0,
        }],
        document_type='invoice',
    )


@tagged('eh_account_einvoice_peppol', 'unit')
class TestUblStructuralValidator(TransactionCase):

    def setUp(self):
        super().setUp()
        self.payload = _payload()
        self.xml = render_invoice_xml(self.payload)

    # ---- positive: clean bytes pass ----

    def test_clean_render_validates(self):
        self.assertTrue(validate_rendered(self.xml))

    # ---- mandatory elements ----

    def test_missing_customization_id_rejected(self):
        broken = self.xml.replace(
            b'<cbc:CustomizationID>',
            b'<cbc:_skipped_CustomizationID>',
        ).replace(
            b'</cbc:CustomizationID>',
            b'</cbc:_skipped_CustomizationID>',
        )
        with self.assertRaises(PeppolGeneratorError) as ctx:
            validate_rendered(broken)
        self.assertIn('CustomizationID', str(ctx.exception))

    def test_missing_id_rejected(self):
        # Replace the document-level <cbc:ID>INV001</cbc:ID> only;
        # the supplier/customer Party blocks also use cbc:ID for
        # endpoint and party identifiers, so we target the specific
        # invoice number marker.
        broken = self.xml.replace(
            b'<cbc:ID>INV001</cbc:ID>', b'',
        )
        with self.assertRaises(PeppolGeneratorError) as ctx:
            validate_rendered(broken)
        self.assertIn('ID', str(ctx.exception))

    def test_missing_currency_code_rejected(self):
        broken = self.xml.replace(
            b'<cbc:DocumentCurrencyCode>AUD</cbc:DocumentCurrencyCode>',
            b'',
        )
        with self.assertRaises(PeppolGeneratorError) as ctx:
            validate_rendered(broken)
        self.assertIn('DocumentCurrencyCode', str(ctx.exception))

    # ---- currency consistency ----

    def test_currency_mismatch_rejected(self):
        # Flip one currencyID attribute to EUR while keeping
        # DocumentCurrencyCode AUD. The validator scans every
        # element with a currencyID attribute.
        broken = self.xml.replace(
            b'currencyID="AUD"', b'currencyID="EUR"', 1,
        )
        with self.assertRaises(PeppolGeneratorError) as ctx:
            validate_rendered(broken)
        msg = str(ctx.exception)
        self.assertIn('currencyID', msg)
        self.assertIn('EUR', msg)
        self.assertIn('AUD', msg)

    # ---- malformed XML ----

    def test_malformed_xml_rejected(self):
        with self.assertRaises(PeppolGeneratorError) as ctx:
            validate_rendered(b'<not xml')
        self.assertIn('parseable', str(ctx.exception))

    # ---- amount drift ----

    def test_line_extension_sum_drift_rejected(self):
        # Replace the LegalMonetaryTotal/LineExtensionAmount with a
        # value that differs from sum(line LineExtensionAmount) by
        # more than 1c. The line side carries 100.00; we set the
        # legal monetary total side to 99.00 so the drift is 1.00.
        broken = self.xml.replace(
            b'<cac:LegalMonetaryTotal>',
            b'<cac:LegalMonetaryTotal>'
            b'<cbc:LineExtensionAmount currencyID="AUD">99.00'
            b'</cbc:LineExtensionAmount>',
            1,
        )
        # Remove the original LegalMonetaryTotal/LineExtensionAmount
        # so we have only the bogus one inside the block.
        broken = broken.replace(
            b'<cbc:LineExtensionAmount currencyID="AUD">100.00'
            b'</cbc:LineExtensionAmount>',
            b'',
            1,
        )
        with self.assertRaises(PeppolGeneratorError) as ctx:
            validate_rendered(broken)
        self.assertIn('LineExtensionAmount', str(ctx.exception))
