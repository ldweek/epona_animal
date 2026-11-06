# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Golden-output regression for the Peppol BIS Billing 3.0 (UBL 2.1) generator.

These lock the exact serialized UBL bytes for a fixed fixture set. They
are the safety net for the eh_edi_core phase-2 unification: any change to
the UBL output flips a golden and must be reviewed, never slips through
silently. The existing generator tests assert structure and round-trip;
these assert the whole document byte for byte.

Regenerate goldens after an intentional change:

    EH_WRITE_GOLDEN=1 odoo --test-tags eh_account_einvoice_peppol \
        -i eh_account_einvoice_peppol

then review the diff under tests/golden/ in the same commit.
"""

import datetime
import os
from pathlib import Path

from odoo.tests import BaseCase, tagged

from odoo.addons.eh_account_einvoice_peppol.tools.ubl_generator import (
    make_invoice_payload,
    render_invoice_xml,
)


GOLDEN_DIR = Path(__file__).resolve().parent / 'golden'


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


def _golden_check(testcase, name, actual_bytes):
    GOLDEN_DIR.mkdir(exist_ok=True)
    path = GOLDEN_DIR / name
    if os.environ.get('EH_WRITE_GOLDEN') == '1':
        path.write_bytes(actual_bytes)
        return
    testcase.assertTrue(
        path.exists(),
        "Golden file missing: %s. Generate it once with "
        "EH_WRITE_GOLDEN=1." % path,
    )
    testcase.assertEqual(
        actual_bytes, path.read_bytes(),
        "UBL output drifted from golden %s. If the change is "
        "intentional, regenerate with EH_WRITE_GOLDEN=1 and review the "
        "diff in the same commit." % name,
    )


@tagged('eh_account_einvoice_peppol', 'golden', 'post_install', '-at_install')
class TestGoldenUBL(BaseCase):

    def _payload(self, **kw):
        return make_invoice_payload(
            invoice_number='INV/2026/0001',
            issue_date=datetime.date(2026, 5, 1),
            due_date=datetime.date(2026, 5, 31),
            currency_code='AUD',
            supplier=_supplier(), customer=_customer(),
            lines=_lines(), tax_categories=_categories(),
            buyer_reference='REF-9001',
            **kw,
        )

    def test_golden_invoice(self):
        xml = render_invoice_xml(self._payload())
        _golden_check(self, 'ubl_invoice.xml', xml)

    def test_golden_credit_note(self):
        xml = render_invoice_xml(self._payload(document_type='credit_note'))
        _golden_check(self, 'ubl_credit_note.xml', xml)
